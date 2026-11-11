"""Revisão humana: decisões e correções por edifício, persistidas fora do pipeline.

As revisões ficam num SQLite próprio (``data/processed/<área>/reviews.sqlite``) e não
no GeoPackage de uma fase. Assim, voltar a correr o pipeline não apaga trabalho
humano. São aplicadas por cima dos resultados, na aplicação de revisão e na
exportação.

Uma revisão fica *desatualizada* se a versão OSM do edifício mudar entre o momento da
revisão e o download atual. Nesse caso volta a "pending" e é sinalizada.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

import numpy as np
import shapely
from pydantic import BaseModel, Field

from osm3d.domain.models import (
    Building,
    BuildingPart,
    Estimate,
    QAFlag,
    ReviewStatus,
    Roof,
    RoofModel,
    RoofShape,
    Source,
)
from osm3d.geometry import axis_difference_deg, direction_difference_deg
from osm3d.reconstruction.roof_models import LocalFrame
from osm3d.reconstruction.skeleton import has_skeleton, renders_as_skeleton

DEFAULT_ROOF_HEIGHT_RATIO = 0.3  # telhado novo sem altura indicada: 30 % da altura total
MAX_DEFAULT_ROOF_HEIGHT_M = 2.5


class Overrides(BaseModel):
    """Correções manuais. ``None`` = manter o valor estimado."""

    roof_shape: RoofShape | None = None
    height: float | None = Field(default=None, gt=0, le=200)
    roof_height: float | None = Field(default=None, ge=0, le=50)
    roof_orientation: Literal["along", "across"] | None = None
    roof_direction: float | None = Field(default=None, ge=0, lt=360)
    roof_colour: str | None = Field(default=None, pattern=r"^#[0-9a-fA-F]{6}$")
    building_colour: str | None = Field(default=None, pattern=r"^#[0-9a-fA-F]{6}$")

    def is_empty(self) -> bool:
        return all(v is None for v in self.model_dump().values())


class Review(BaseModel):
    osm_key: str
    osm_version: int
    status: ReviewStatus = "pending"
    note: str | None = None
    overrides: Overrides = Field(default_factory=Overrides)
    reviewed_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class ReviewStore:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        with self._conn() as c:
            c.execute(
                "CREATE TABLE IF NOT EXISTS reviews ("
                " osm_key TEXT PRIMARY KEY, osm_version INTEGER NOT NULL,"
                " status TEXT NOT NULL, note TEXT, overrides TEXT NOT NULL,"
                " reviewed_at TEXT NOT NULL)"
            )

    def _conn(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path)

    def all(self) -> dict[str, Review]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT osm_key, osm_version, status, note, overrides, reviewed_at FROM reviews"
            ).fetchall()
        return {
            r[0]: Review(
                osm_key=r[0],
                osm_version=r[1],
                status=r[2],
                note=r[3],
                overrides=Overrides.model_validate_json(r[4]),
                reviewed_at=datetime.fromisoformat(r[5]),
            )
            for r in rows
        }

    def save(self, review: Review) -> None:
        with self._conn() as c:
            c.execute(
                "INSERT OR REPLACE INTO reviews VALUES (?, ?, ?, ?, ?, ?)",
                (
                    review.osm_key,
                    review.osm_version,
                    review.status,
                    review.note,
                    review.overrides.model_dump_json(),
                    review.reviewed_at.isoformat(),
                ),
            )

    def delete(self, osm_key: str) -> None:
        with self._conn() as c:
            c.execute("DELETE FROM reviews WHERE osm_key = ?", (osm_key,))


# ------------------------------------------------------------------- correções


def _shifted_footprint(b: Building):  # type: ignore[no-untyped-def]
    dx, dy = b.registration_offset_m
    return shapely.transform(b.footprint, lambda c: c + np.array([dx, dy]))


def _facing(shape: RoofShape, frame: LocalFrame, flip: bool) -> float | None:
    """``roof:direction`` implícito: água (skillion) ou ponta em hip (side_hipped)."""
    turn = 180.0 if flip else 0.0
    if shape is RoofShape.SKILLION:
        return (frame.v_azimuth_deg + turn) % 360.0
    if shape is RoofShape.SIDE_HIPPED:
        return (frame.azimuth_deg + turn) % 360.0
    return None


def _directed_frame(shape: RoofShape, base: LocalFrame, facing: float) -> tuple[LocalFrame, bool]:
    """Referencial e sentido cuja direção fica o mais perto possível de ``facing``."""
    options = [(f, flip) for f in (base, base.rotated()) for flip in (False, True)]
    return min(
        options,
        key=lambda o: direction_difference_deg(_facing(shape, o[0], o[1]) or 0.0, facing),
    )


def apply_overrides(b: Building, part: BuildingPart, ov: Overrides) -> BuildingPart:
    """Nova parte com as correções aplicadas (e o modelo 3D coerente com elas)."""
    if ov.is_empty():
        return part
    manual = Source.MANUAL
    roof = part.roof
    shape = ov.roof_shape or roof.shape.value
    height = ov.height if ov.height is not None else part.height.value

    if shape in (RoofShape.FLAT, RoofShape.UNKNOWN):
        roof_h = 0.0
    elif ov.roof_height is not None:
        roof_h = ov.roof_height
    elif roof.height is not None and roof.shape.value not in (RoofShape.FLAT, RoofShape.UNKNOWN):
        roof_h = roof.height.value
    else:
        roof_h = min(MAX_DEFAULT_ROOF_HEIGHT_M, DEFAULT_ROOF_HEIGHT_RATIO * height)
    roof_h = min(roof_h, height)

    geometry_unchanged = (
        roof.model is not None
        and ov.roof_shape in (None, roof.shape.value)
        and ov.roof_orientation is None
        and ov.roof_direction is None
    )
    shifted = _shifted_footprint(b)
    base = LocalFrame.from_geometry(shifted)
    flip, ridge_ratio, ridge_offset = False, None, None
    if geometry_unchanged:
        assert roof.model is not None
        m = roof.model
        frame = LocalFrame(m.cx, m.cy, m.azimuth_deg, m.half_length, m.half_width)
        flip, ridge_ratio, ridge_offset = m.flip, m.ridge_ratio, m.ridge_offset
    elif shape in (RoofShape.SKILLION, RoofShape.SIDE_HIPPED):
        facing = ov.roof_direction
        if facing is None:
            facing = roof.facing_azimuth_deg if roof.facing_azimuth_deg is not None else 0.0
        frame, flip = _directed_frame(shape, base, facing)
    else:
        across = ov.roof_orientation == "across"
        if ov.roof_orientation is None and roof.ridge_azimuth_deg is not None:
            across = axis_difference_deg(roof.ridge_azimuth_deg, base.azimuth_deg) > 45
        frame = base.rotated() if across else base

    ground = b.ground_elevation_m
    model = None
    if ground is not None and shape is not RoofShape.UNKNOWN:
        model = RoofModel(
            cx=frame.cx,
            cy=frame.cy,
            azimuth_deg=frame.azimuth_deg,
            half_length=frame.half_length,
            half_width=frame.half_width,
            flip=flip,
            ridge_ratio=(
                ridge_ratio if shape in (RoofShape.HIPPED, RoofShape.SIDE_HIPPED) else None
            ),
            ridge_offset=ridge_offset if shape is RoofShape.GABLED else None,
            # Planta não retangular: gabled/hipped seguem o esqueleto (como no renderizador).
            skeleton=renders_as_skeleton(shifted, shape) and has_skeleton(shifted, shape),
            eave_z=ground + height - roof_h,
            roof_height=roof_h,
        )

    def est(value: float, original: Estimate[float] | None, overridden: bool) -> Estimate[float]:
        if overridden or original is None:
            return Estimate[float](value=value, confidence=1.0, source=manual)
        return original.model_copy(update={"value": value})

    shape_changed = ov.roof_shape is not None
    new_roof = Roof(
        shape=(
            Estimate[RoofShape](value=shape, confidence=1.0, source=manual)
            if shape_changed
            else roof.shape
        ),
        height=(
            None
            if shape in (RoofShape.FLAT, RoofShape.UNKNOWN)
            else est(roof_h, roof.height, ov.roof_height is not None or shape_changed)
        ),
        ridge_azimuth_deg=(
            frame.azimuth_deg % 180.0
            if shape in (RoofShape.GABLED, RoofShape.HIPPED, RoofShape.SIDE_HIPPED)
            else None
        ),
        facing_azimuth_deg=_facing(shape, frame, flip),
        colour=(
            Estimate[str](value=ov.roof_colour.lower(), confidence=1.0, source=manual)
            if ov.roof_colour
            else roof.colour
        ),
        rmse_m=roof.rmse_m,
        model=model,
    )
    facade = part.facade_colour
    if ov.building_colour:
        facade = Estimate[str](value=ov.building_colour.lower(), confidence=1.0, source=manual)
    return part.model_copy(
        update={
            "height": est(height, part.height, ov.height is not None),
            "roof": new_roof,
            "facade_colour": facade,
        }
    )


def apply_review(b: Building, review: Review | None) -> Building:
    """Edifício com a decisão e as correções da revisão aplicadas."""
    if review is None:
        return b
    if review.osm_version != b.osm_version:
        flag = QAFlag(
            code="review_stale",
            message=f"Revisto na versão {review.osm_version}; a versão OSM atual é {b.osm_version}",
        )
        return b.model_copy(update={"qa_flags": [*b.qa_flags, flag]})
    parts = b.parts
    if parts and not review.overrides.is_empty():
        parts = [apply_overrides(b, parts[0], review.overrides), *parts[1:]]
    return b.model_copy(
        update={"review_status": review.status, "review_note": review.note, "parts": parts}
    )


def orientation_of(b: Building, part: BuildingPart) -> Literal["along", "across"] | None:
    """Orientação S3DB da cumeeira face ao lado maior (como em ``roof:orientation``)."""
    if part.roof.ridge_azimuth_deg is None:
        return None
    long_axis = LocalFrame.from_geometry(b.footprint).azimuth_deg
    return "across" if axis_difference_deg(part.roof.ridge_azimuth_deg, long_axis) > 45 else "along"
