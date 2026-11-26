"""Revisão humana: decisões e correções por edifício, persistidas fora do pipeline.

As revisões ficam num SQLite próprio (``data/processed/<área>/reviews.sqlite``) e não
no GeoPackage de uma fase. Assim, voltar a correr o pipeline não apaga trabalho
humano. São aplicadas por cima dos resultados, na aplicação de revisão e na
exportação.

Uma revisão fica *desatualizada* se a versão OSM do edifício mudar entre o momento da
revisão e o download atual. Nesse caso volta a "pending" e é sinalizada.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

import numpy as np
import shapely
from pydantic import BaseModel, Field, field_validator
from shapely.geometry.base import BaseGeometry

from osm3d.domain.models import (
    Building,
    BuildingPart,
    Estimate,
    GroundChoice,
    PartsLayout,
    QAFlag,
    ReviewStatus,
    Roof,
    RoofModel,
    RoofShape,
    Source,
    TopologyMode,
)
from osm3d.geometry import axis_difference_deg, direction_difference_deg
from osm3d.geometry_edits import GeometryEdits
from osm3d.reconstruction.roof_models import LocalFrame
from osm3d.reconstruction.skeleton import has_skeleton, renders_as_skeleton

DEFAULT_ROOF_HEIGHT_RATIO = 0.3  # telhado novo sem altura indicada: 30 % da altura total
MAX_DEFAULT_ROOF_HEIGHT_M = 2.5


# Valores de ``building`` que a revisão pode escolher para um edifício novo.
BUILDING_TYPES: dict[str, str] = {
    "yes": "edifício (genérico)",
    "house": "moradia",
    "detached": "moradia isolada",
    "semidetached_house": "moradia geminada",
    "terrace": "moradias em banda",
    "apartments": "prédio de apartamentos",
    "residential": "residencial",
    "garage": "garagem",
    "garages": "garagens",
    "shed": "arrecadação / anexo",
    "roof": "telheiro / alpendre",
    "carport": "telheiro para carros",
    "hut": "barraca",
    "farm_auxiliary": "anexo agrícola",
    "barn": "palheiro / celeiro",
    "greenhouse": "estufa",
    "industrial": "indústria / fábrica",
    "warehouse": "armazém",
    "commercial": "comércio / serviços",
    "retail": "loja",
    "service": "técnico (posto de transformação…)",
    "church": "igreja",
    "chapel": "capela",
    "school": "escola",
    "public": "público",
    "ruins": "ruína",
}


class Overrides(BaseModel):
    """Correções manuais. ``None`` = manter o valor estimado."""

    # Só em edifícios novos (candidatos): tipo ``building``. Nos do OSM não se mexe.
    building: str | None = None
    roof_shape: RoofShape | None = None
    height: float | None = Field(default=None, gt=0, le=200)
    roof_height: float | None = Field(default=None, ge=0, le=50)
    roof_orientation: Literal["along", "across"] | None = None
    roof_direction: float | None = Field(default=None, ge=0, lt=360)
    roof_colour: str | None = Field(default=None, pattern=r"^#[0-9a-fA-F]{6}$")
    building_colour: str | None = Field(default=None, pattern=r"^#[0-9a-fA-F]{6}$")
    # Encosta: medir height a partir do chão baixo ou do terreno do lote.
    ground: GroundChoice | None = None
    # Só para partes: base da parte acima do terreno (volumes suspensos/sobrepostos).
    min_height: float | None = Field(default=None, ge=0, le=200)
    # Só em edifícios novos: sobreposições com o OSM (ajustar o OSM, recortar, manter).
    topology: TopologyMode | None = None

    @field_validator("building")
    @classmethod
    def _known_building(cls, v: str | None) -> str | None:
        if v is not None and v not in BUILDING_TYPES:
            raise ValueError(f"building={v!r} desconhecido (opções: {', '.join(BUILDING_TYPES)})")
        return v

    def is_empty(self) -> bool:
        return all(v is None for v in self.model_dump().values())


class Review(BaseModel):
    osm_key: str
    osm_version: int
    status: ReviewStatus = "pending"
    note: str | None = None
    overrides: Overrides = Field(default_factory=Overrides)
    reviewed_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    # Arquivada: aceite já submetido ao OSM, ou rejeitado que não se quer voltar a ver.
    # Sai da lista/mapa e das exportações; a decisão mantém-se.
    archived_at: datetime | None = None
    # Divisão em partes (com o ajuste de cada parte) e correções por parte.
    parts: PartsLayout | None = None
    part_overrides: list[Overrides] = Field(default_factory=list)

    @property
    def archived(self) -> bool:
        return self.archived_at is not None


_COLUMNS = "osm_key, osm_version, status, note, overrides, reviewed_at, archived_at, parts"


class _PartsRecord(BaseModel):
    layout: PartsLayout
    overrides: list[Overrides] = Field(default_factory=list)


def _parts_to_json(review: Review) -> str | None:
    if review.parts is None:
        return None
    return _PartsRecord(layout=review.parts, overrides=review.part_overrides).model_dump_json()


def _parts_from_json(raw: str | None) -> tuple[PartsLayout | None, list[Overrides]]:
    if not raw:
        return None, []
    rec = _PartsRecord.model_validate_json(raw)
    return rec.layout, rec.overrides


class ReviewStore:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        with self._conn() as c:
            c.execute(
                "CREATE TABLE IF NOT EXISTS reviews ("
                " osm_key TEXT PRIMARY KEY, osm_version INTEGER NOT NULL,"
                " status TEXT NOT NULL, note TEXT, overrides TEXT NOT NULL,"
                " reviewed_at TEXT NOT NULL, archived_at TEXT, parts TEXT)"
            )
            cols = {r[1] for r in c.execute("PRAGMA table_info(reviews)")}
            for col in ("archived_at", "parts"):  # bases de dados de versões anteriores
                if col not in cols:
                    c.execute(f"ALTER TABLE reviews ADD COLUMN {col} TEXT")
            # Edições de geometria no mapa (landuse do OSM, muros): ver geometry_edits.py.
            c.execute(
                "CREATE TABLE IF NOT EXISTS geometry_edits ("
                " key TEXT PRIMARY KEY, data TEXT NOT NULL, edited_at TEXT NOT NULL)"
            )
            # Reconstrução de um edifício com o contorno editado (ver footprint_edits.py):
            # assinatura do contorno + edifício reajustado (JSON).
            c.execute(
                "CREATE TABLE IF NOT EXISTS footprint_fits ("
                " key TEXT PRIMARY KEY, signature TEXT NOT NULL, data TEXT NOT NULL)"
            )

    def _conn(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path)

    def all(self) -> dict[str, Review]:
        with self._conn() as c:
            rows = c.execute(f"SELECT {_COLUMNS} FROM reviews").fetchall()
        out: dict[str, Review] = {}
        for r in rows:
            layout, part_overrides = _parts_from_json(r[7])
            out[r[0]] = Review(
                osm_key=r[0],
                osm_version=r[1],
                status=r[2],
                note=r[3],
                overrides=Overrides.model_validate_json(r[4]),
                reviewed_at=datetime.fromisoformat(r[5]),
                archived_at=datetime.fromisoformat(r[6]) if r[6] else None,
                parts=layout,
                part_overrides=part_overrides,
            )
        return out

    def save(self, review: Review) -> None:
        with self._conn() as c:
            c.execute(
                f"INSERT OR REPLACE INTO reviews ({_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    review.osm_key,
                    review.osm_version,
                    review.status,
                    review.note,
                    review.overrides.model_dump_json(),
                    review.reviewed_at.isoformat(),
                    review.archived_at.isoformat() if review.archived_at else None,
                    _parts_to_json(review),
                ),
            )

    def set_archived(self, status: ReviewStatus, archived: bool) -> int:
        """(Des)arquiva todas as revisões com ``status``. Devolve o n.º de alteradas."""
        value = datetime.now(UTC).isoformat() if archived else None
        cond = "archived_at IS NULL" if archived else "archived_at IS NOT NULL"
        with self._conn() as c:
            cur = c.execute(
                f"UPDATE reviews SET archived_at = ? WHERE status = ? AND {cond}", (value, status)
            )
            return cur.rowcount

    def delete(self, osm_key: str) -> None:
        with self._conn() as c:
            c.execute("DELETE FROM reviews WHERE osm_key = ?", (osm_key,))

    def geometry_edits(self) -> GeometryEdits:
        """Edições de geometria feitas no mapa (landuse e muros)."""
        with self._conn() as c:
            rows = c.execute("SELECT key, data FROM geometry_edits").fetchall()
        return GeometryEdits.from_records({k: json.loads(d) for k, d in rows})

    def save_geometry_edits(self, edits: GeometryEdits) -> None:
        """Substitui as edições guardadas (só as chaves que mudaram são reescritas)."""
        new = {k: json.dumps(v) for k, v in edits.records().items()}
        now = datetime.now(UTC).isoformat()
        with self._conn() as c:
            old = dict(c.execute("SELECT key, data FROM geometry_edits").fetchall())
            gone = [(k,) for k in old.keys() - new.keys()]
            c.executemany("DELETE FROM geometry_edits WHERE key = ?", gone)
            c.executemany(
                "INSERT OR REPLACE INTO geometry_edits (key, data, edited_at) VALUES (?, ?, ?)",
                [(k, d, now) for k, d in new.items() if old.get(k) != d],
            )

    def footprint_fits(self) -> dict[str, tuple[str, str]]:
        """Edifícios reajustados a um contorno editado: chave → (assinatura, JSON)."""
        with self._conn() as c:
            rows = c.execute("SELECT key, signature, data FROM footprint_fits").fetchall()
        return {k: (s, d) for k, s, d in rows}

    def save_footprint_fit(self, key: str, signature: str, data: str) -> None:
        with self._conn() as c:
            c.execute(
                "INSERT OR REPLACE INTO footprint_fits (key, signature, data) VALUES (?, ?, ?)",
                (key, signature, data),
            )

    def delete_footprint_fits(self, keys: list[str]) -> None:
        with self._conn() as c:
            c.executemany("DELETE FROM footprint_fits WHERE key = ?", [(k,) for k in keys])


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


def apply_overrides(
    b: Building, part: BuildingPart, ov: Overrides, geometry: BaseGeometry | None = None
) -> BuildingPart:
    """Nova parte com as correções aplicadas (e o modelo 3D coerente com elas).

    ``geometry``: planta da parte no referencial do LiDAR (por omissão, o contorno do
    edifício deslocado pelo co-registo; para partes, a planta da parte).
    """
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
    shifted = geometry if geometry is not None else _shifted_footprint(b)
    base = LocalFrame.from_geometry(shifted)
    flip, ridge_ratio, ridge_offset, ridge_shift = False, None, None, None
    if geometry_unchanged:
        assert roof.model is not None
        m = roof.model
        frame = LocalFrame(m.cx, m.cy, m.azimuth_deg, m.half_length, m.half_width)
        flip, ridge_ratio, ridge_offset = m.flip, m.ridge_ratio, m.ridge_offset
        ridge_shift = m.ridge_shift
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
            ridge_shift=ridge_shift if shape is RoofShape.HIPPED else None,
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
            if shape in (RoofShape.GABLED, RoofShape.HIPPED, RoofShape.SIDE_HIPPED, RoofShape.ROUND)
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
            "min_height": ov.min_height if ov.min_height is not None else part.min_height,
            "roof": new_roof,
            "facade_colour": facade,
        }
    )


def apply_review(b: Building, review: Review | None) -> Building:
    """Edifício com a decisão e as correções da revisão aplicadas."""
    if review is None:
        return b
    if review.archived:
        # Arquivado: a decisão está fechada. Depois de submeter, a versão OSM muda por
        # causa da própria edição; isso não torna a revisão desatualizada.
        b = apply_corrections(b, review.overrides)
        if review.parts is not None:
            b = apply_parts(b, review.parts, review.part_overrides)
        return b.model_copy(
            update={
                "review_status": review.status,
                "review_note": review.note,
                "review_archived": True,
            }
        )
    if review.osm_version != b.osm_version:
        flag = QAFlag(
            code="review_stale",
            message=f"Revisto na versão {review.osm_version}; a versão OSM atual é {b.osm_version}",
        )
        return b.model_copy(update={"qa_flags": [*b.qa_flags, flag]})
    b = apply_corrections(b, review.overrides)
    if review.parts is not None:
        b = apply_parts(b, review.parts, review.part_overrides)
    return b.model_copy(update={"review_status": review.status, "review_note": review.note})


def apply_ground(b: Building, choice: GroundChoice) -> Building:
    """Muda a cota do terreno de referência mantendo o topo do telhado no mesmo sítio."""
    if b.ground is None or b.ground_elevation_m is None or b.ground.choice == choice:
        return b
    new_ground = b.ground.elevation(choice)
    delta = b.ground_elevation_m - new_ground
    parts = [
        p.model_copy(
            update={
                "height": Estimate[float](
                    value=p.height.value + delta, confidence=1.0, source=Source.MANUAL
                )
            }
        )
        for p in b.parts
    ]
    ground = b.ground.model_copy(
        update={"choice": choice, "basis": "manual", "reason": "escolhido na revisão"}
    )
    flags = [f for f in b.qa_flags if f.code != "terrain_step"]
    return b.model_copy(
        update={
            "ground_elevation_m": new_ground,
            "ground": ground,
            "parts": parts,
            "qa_flags": flags,
        }
    )


def apply_parts(b: Building, layout: PartsLayout, overrides: list[Overrides]) -> Building:
    """Edifício dividido em partes: cada parte com o seu ajuste e as suas correções."""
    if not layout.fitted or len(layout.fitted) != len(layout.parts):
        return b
    dx, dy = b.registration_offset_m
    parts = []
    for i, part in enumerate(layout.fitted):
        ov = overrides[i] if i < len(overrides) else Overrides()
        geom = shapely.transform(layout.polygon(i), lambda c: c + np.array([dx, dy]))
        parts.append(apply_overrides(b, part, ov, geometry=geom))
    return b.model_copy(update={"parts": parts, "layout": layout})


def apply_corrections(b: Building, ov: Overrides) -> Building:
    """Aplica as correções (terreno primeiro, depois telhado/alturas/cores)."""
    if ov.ground is not None:
        b = apply_ground(b, ov.ground)
    if ov.building is not None and b.osm_type == "new":
        b = b.model_copy(update={"existing_tags": {**b.existing_tags, "building": ov.building}})
    if ov.topology is not None and b.osm_type == "new":
        b = b.model_copy(update={"topology_mode": ov.topology})
    if b.parts and not ov.model_copy(update={"building": None, "topology": None}).is_empty():
        b = b.model_copy(update={"parts": [apply_overrides(b, b.parts[0], ov), *b.parts[1:]]})
    return b


def orientation_of(b: Building, part: BuildingPart) -> Literal["along", "across"] | None:
    """Orientação S3DB da cumeeira face ao lado maior (como em ``roof:orientation``)."""
    if part.roof.ridge_azimuth_deg is None:
        return None
    long_axis = LocalFrame.from_geometry(b.footprint).azimuth_deg
    return "across" if axis_difference_deg(part.roof.ridge_azimuth_deg, long_axis) > 45 else "along"
