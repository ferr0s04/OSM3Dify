"""Lógica da aplicação de revisão, independente do framework web."""

from __future__ import annotations

import math
from collections import Counter
from pathlib import Path
from typing import Any

import numpy as np
import shapely
from PIL import ImageColor
from pyproj import Transformer
from shapely.geometry import mapping

from osm3d.app.points import BUILDING_CLASS, TilePointCache
from osm3d.appearance.roof_colours import DEFAULT_FACADE_HEX, DEFAULT_ROOF_HEX, PALETTE
from osm3d.config import Config
from osm3d.domain.models import Building, ReviewStatus, RoofShape
from osm3d.evaluation.benchmark import evaluate, normalize_osm_shape
from osm3d.export.geopackage import read_buildings
from osm3d.export.mesh import roof_mesh, wall_mesh
from osm3d.export.osm_changes import ChangeSummary, classify_change
from osm3d.export.osm_xml import plan_export
from osm3d.export.s3db_tags import TagMerge, merge_tags, part_to_tags
from osm3d.geospatial.point_clouds import index_tiles
from osm3d.pipeline import (
    Stage,
    area_dir,
    artefact_path,
    export_area,
    review_store_path,
    tag_policy,
)
from osm3d.reconstruction.skeleton import has_skeleton, renders_as_skeleton
from osm3d.review import (
    Overrides,
    Review,
    ReviewStore,
    apply_overrides,
    apply_review,
    orientation_of,
)

MAX_BUILDING_POINTS = 40_000
MAX_CONTEXT_POINTS = 15_000
CONTEXT_MARGIN_M = 4.0


def round_to(value: float, step: float) -> float:
    return round(round(value / step) * step, 2) if step > 0 else round(value, 2)


def display_colour(value: str, default: str) -> str:
    """Cor OSM (nome CSS, hex, ou variantes como ``light_gray``) → #rrggbb para mostrar.

    Só para visualização: as tags não são alteradas.
    """
    for candidate in (value, value.replace("_", ""), value.replace("_", "").replace(" ", "")):
        try:
            r, g, b = ImageColor.getrgb(candidate.strip().lower())[:3]
            return f"#{r:02x}{g:02x}{b:02x}"
        except ValueError:
            continue
    return default


def _num(v: float | None, nd: int = 2) -> float | None:
    return None if v is None or not math.isfinite(v) else round(float(v), nd)


class NotFound(KeyError):
    pass


class ReviewService:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.source = next(
            (p for s in (Stage.QA, Stage.ROOFS) if (p := artefact_path(cfg, s)).exists()), None
        )
        if self.source is None:
            raise FileNotFoundError("Sem resultados: correr primeiro `osm3d run --to qa`")
        self.base: dict[str, Building] = {b.key: b for b in read_buildings(self.source)}
        self.store = ReviewStore(review_store_path(cfg))
        self.reviews: dict[str, Review] = self.store.all()
        self.points = TilePointCache(
            index_tiles(cfg.sources.lidar_dir, (".laz", ".las")), area_dir(cfg) / "cache"
        )
        self.policy = tag_policy(cfg)
        self._to_wgs84 = Transformer.from_crs(cfg.processing_crs, "EPSG:4326", always_xy=True)
        self._wgs84: dict[str, Any] = {}

    # ------------------------------------------------------------------ básico

    def _h(self, v: float) -> float:
        """Altura arredondada como na exportação (0,5 m por omissão)."""
        return round_to(v, self.cfg.export.height_rounding_m)

    def _rh(self, v: float) -> float:
        return round_to(v, self.cfg.export.roof_height_rounding_m)

    def building(self, key: str) -> Building:
        if key not in self.base:
            raise NotFound(key)
        return apply_review(self.base[key], self.reviews.get(key))

    def all_buildings(self) -> list[Building]:
        return [self.building(k) for k in self.base]

    def _geom_wgs84(self, key: str) -> Any:
        if key not in self._wgs84:
            g = self.base[key].footprint
            self._wgs84[key] = shapely.transform(
                g, lambda c: np.column_stack(self._to_wgs84.transform(c[:, 0], c[:, 1]))
            )
        return self._wgs84[key]

    def _row(self, b: Building) -> dict[str, Any]:
        part = b.parts[0] if b.parts else None
        shape = part.roof.shape.value.value if part else None
        conf = part.roof.shape.confidence if part else None
        exportable = (
            shape
            if part
            and conf is not None
            and conf >= self.policy.min_shape_confidence
            and shape != "unknown"
            else "unknown"
        )
        osm_shape = normalize_osm_shape(b.existing_tags.get("roof:shape"))
        c = self._geom_wgs84(b.key).centroid
        review = self.reviews.get(b.key)
        colours = self._colours(b)
        change = self._change(b)
        return {
            "roof_colour": colours["roof"],
            "building_colour": colours["walls"],
            # Relação com o OSM: já tem 3D mapeado? A análise acrescenta ou diverge?
            "osm3d": change.has_osm_3d,
            "change": change.kind.value,
            "n_added": len(change.added),
            "n_conflicts": len(change.conflicts),
            "osm_parts": change.osm_parts,
            "key": b.key,
            "status": b.review_status,
            "needs_review": b.needs_review,
            "flags": [f.code for f in b.qa_flags if f.severity != "info"],
            "info_flags": [f.code for f in b.qa_flags if f.severity == "info"],
            "shape": exportable if part else None,
            "shape_conf": _num(conf),
            "height": self._h(part.height.value) if part else None,
            "roof_height": self._rh(part.roof.height.value) if part and part.roof.height else None,
            "osm_roof_shape": b.existing_tags.get("roof:shape"),
            "match": (osm_shape == exportable) if osm_shape and exportable != "unknown" else None,
            "edited": bool(review and not review.overrides.is_empty()),
            "area": _num(b.footprint.area, 0),
            "lon": round(c.x, 7),
            "lat": round(c.y, 7),
        }

    def rows(self) -> list[dict[str, Any]]:
        return [self._row(b) for b in self.all_buildings()]

    def geojson(self) -> dict[str, Any]:
        feats: list[dict[str, Any]] = []
        for b in self.all_buildings():
            row = self._row(b)
            feats.append(
                {
                    "type": "Feature",
                    "id": len(feats),
                    "geometry": mapping(self._geom_wgs84(b.key)),
                    "properties": {
                        k: row[k]
                        for k in (
                            "key",
                            "status",
                            "shape",
                            "shape_conf",
                            "height",
                            "needs_review",
                            "match",
                            "roof_colour",
                            "building_colour",
                            "osm3d",
                            "change",
                        )
                    }
                    | {"flags": ",".join(row["flags"])},
                }
            )
        return {"type": "FeatureCollection", "features": feats}

    # ----------------------------------------------------------------- detalhe

    def _merge(self, b: Building) -> tuple[dict[str, str], TagMerge] | None:
        if not b.parts:
            return None
        proposed = part_to_tags(b.parts[0], self.policy)
        merge = merge_tags(
            b.existing_tags, proposed, overwrite=self.cfg.export.overwrite_existing_tags
        )
        return proposed, merge

    def _change(self, b: Building) -> ChangeSummary:
        m = self._merge(b)
        return classify_change(b.existing_tags, m[1] if m else None, len(b.osm_parts))

    def _tags(self, b: Building) -> dict[str, Any]:
        m = self._merge(b)
        if m is None:
            return {"proposed": {}, "final": b.existing_tags, "added": {}, "conflicts": {}}
        proposed, merge = m
        return {
            "proposed": proposed,
            "final": merge.tags,
            "added": merge.added,
            "conflicts": {k: {"osm": a, "proposed": p} for k, (a, p) in merge.conflicts.items()},
        }

    def _colours(self, b: Building) -> dict[str, str]:
        """Cores que vão ser aplicadas (tags finais), com a origem de cada uma.

        Sem tag, usa-se a cor por omissão dos renderizadores (só para visualizar).
        """
        tags = self._tags(b)
        out: dict[str, str] = {}
        for name, tag, default in (
            ("roof", "roof:colour", DEFAULT_ROOF_HEX),
            ("walls", "building:colour", DEFAULT_FACADE_HEX),
        ):
            value = tags["final"].get(tag)
            if value is None:
                out[name], out[f"{name}_source"] = default, "omissão"
            elif tag in b.existing_tags and tag not in tags["added"]:
                out[name], out[f"{name}_source"] = display_colour(value, default), "OSM"
                out[f"{name}_tag"] = value
            else:
                part = b.parts[0]
                est = part.roof.colour if name == "roof" else part.facade_colour
                out[name] = display_colour(value, default)
                out[f"{name}_source"] = (
                    "manual" if est and est.source.value == "manual" else "estimada"
                )
        return out

    def _part_info(self, b: Building) -> dict[str, Any] | None:
        if not b.parts:
            return None
        p = b.parts[0]
        r = p.roof
        return {
            "roof_colour": r.colour.value if r.colour else None,
            "roof_colour_conf": _num(r.colour.confidence) if r.colour else None,
            "building_colour": p.facade_colour.value if p.facade_colour else None,
            "roof_shape": r.shape.value.value,
            "roof_shape_conf": _num(r.shape.confidence),
            "roof_shape_source": r.shape.source.value,
            "height": self._h(p.height.value),
            "height_conf": _num(p.height.confidence),
            "height_source": p.height.source.value,
            "roof_height": self._rh(r.height.value) if r.height else None,
            "roof_orientation": orientation_of(b, p),
            "ridge_azimuth": _num(r.ridge_azimuth_deg, 1),
            "roof_direction": _num(r.facing_azimuth_deg, 0),
            "rmse_m": _num(r.rmse_m, 3),
            "ridge_ratio": _num(r.model.ridge_ratio) if r.model else None,
        }

    def detail(self, key: str) -> dict[str, Any]:
        b = self.building(key)
        base = self.base[key]
        review = self.reviews.get(key)
        return {
            **self._row(b),
            "osm_type": b.osm_type,
            "osm_id": b.osm_id,
            "osm_version": b.osm_version,
            "osm_url": f"https://www.openstreetmap.org/{b.osm_type}/{b.osm_id}",
            "note": b.review_note,
            "overrides": review.overrides.model_dump() if review else Overrides().model_dump(),
            "reviewed_at": review.reviewed_at.isoformat() if review else None,
            "qa_flags": [f.model_dump() for f in b.qa_flags],
            "diagnostics": {k: _num(v, 3) for k, v in b.diagnostics.items()},
            "existing_tags": b.existing_tags,
            "tags": self._tags(b),
            "colours": self._colours(b),
            "osm_change": {
                "kind": (ch := self._change(b)).kind.value,
                "existing_3d": ch.existing_3d,
                "osm_parts": [f"way/{i}" for i in b.osm_parts],
            },
            "estimated": self._part_info(base),
            "effective": self._part_info(b),
            "ground_m": _num(b.ground_elevation_m),
            "offset": list(b.registration_offset_m),
            "bbox": list(self._geom_wgs84(key).bounds),
        }

    # ---------------------------------------------------------------------- 3D

    def _shifted(self, b: Building) -> Any:
        dx, dy = b.registration_offset_m
        return shapely.transform(b.footprint, lambda c: c + np.array([dx, dy]))

    def _origin(self, b: Building) -> tuple[float, float, float]:
        c = self._shifted(b).centroid
        return (c.x, c.y, b.ground_elevation_m or 0.0)

    def mesh_for(self, b: Building) -> dict[str, Any]:
        fp = self._shifted(b)
        origin = self._origin(b)
        rings = [
            (np.asarray(r.coords)[:, :2] - origin[:2]).round(2).tolist()
            for poly in getattr(fp, "geoms", [fp])
            for r in (poly.exterior, *poly.interiors)
        ]
        out: dict[str, Any] = {
            "origin": origin,
            "footprint": rings,
            "roof": None,
            "walls": None,
            "colours": self._colours(b),
        }
        if not b.parts or b.ground_elevation_m is None:
            return out
        p = b.parts[0]
        shape = p.roof.shape.value
        ground = b.ground_elevation_m
        # O modelo mostrado usa as alturas arredondadas que vão ser exportadas.
        height = self._h(p.height.value)
        flat_z = ground + height
        model = p.roof.model
        if shape is RoofShape.UNKNOWN:
            model = None
        elif model is not None:
            roof_h = 0.0 if shape is RoofShape.FLAT else self._rh(model.roof_height)
            # Desenha-se o que os renderizadores (F4 Map, OSM2World) vão mostrar a partir
            # das tags: em plantas não retangulares, gabled/hipped seguem o esqueleto,
            # mesmo que o ajuste tenha usado outro modelo (p.ex. volume principal).
            skeleton = renders_as_skeleton(fp, shape) and has_skeleton(fp, shape)
            model = model.model_copy(
                update={
                    "roof_height": roof_h,
                    "eave_z": ground + height - roof_h,
                    "skeleton": skeleton,
                }
            )
        resolution = 0.25 if model is not None and model.skeleton else 0.5
        out["roof"] = roof_mesh(fp, model, shape, flat_z, resolution).to_json(origin)
        out["walls"] = wall_mesh(fp, model, shape, ground, flat_z, resolution).to_json(origin)
        return out

    def mesh(self, key: str) -> dict[str, Any]:
        return self.mesh_for(self.building(key))

    def points_for(self, key: str) -> dict[str, Any]:
        b = self.building(key)
        fp = self._shifted(b)
        minx, miny, maxx, maxy = fp.bounds
        m = CONTEXT_MARGIN_M
        ps = self.points.around((minx - m, miny - m, maxx + m, maxy + m))
        if len(ps) == 0:
            return {
                "origin": self._origin(b),
                "count": 0,
                "positions": [],
                "colors": [],
                "classes": [],
                "inside": [],
            }
        inside = (ps.classification == BUILDING_CLASS) & shapely.contains_xy(
            fp, ps.xyz[:, 0], ps.xyz[:, 1]
        )
        rng = np.random.default_rng(0)
        idx_in = np.flatnonzero(inside)
        idx_out = np.flatnonzero(~inside)
        if len(idx_in) > MAX_BUILDING_POINTS:
            idx_in = rng.choice(idx_in, MAX_BUILDING_POINTS, replace=False)
        if len(idx_out) > MAX_CONTEXT_POINTS:
            idx_out = rng.choice(idx_out, MAX_CONTEXT_POINTS, replace=False)
        idx = np.concatenate([idx_in, idx_out])
        origin = self._origin(b)
        xyz = ps.xyz[idx] - np.asarray(origin)
        rgb = ps.rgb[idx] if ps.rgb is not None else np.full((len(idx), 3), 200)
        return {
            "origin": origin,
            "count": int(len(idx)),
            "positions": np.round(xyz, 2).ravel().tolist(),
            "colors": np.clip(rgb, 0, 255).astype(int).ravel().tolist(),
            "classes": ps.classification[idx].astype(int).tolist(),
            "inside": inside[idx].astype(int).tolist(),
        }

    # ------------------------------------------------------------------ revisão

    def preview(self, key: str, overrides: Overrides) -> dict[str, Any]:
        base = self.base.get(key)
        if base is None:
            raise NotFound(key)
        b = base
        if base.parts:
            b = base.model_copy(update={"parts": [apply_overrides(base, base.parts[0], overrides)]})
        return {"mesh": self.mesh_for(b), "tags": self._tags(b), "effective": self._part_info(b)}

    def save_review(
        self, key: str, status: ReviewStatus, note: str | None, overrides: Overrides
    ) -> dict[str, Any]:
        base = self.base.get(key)
        if base is None:
            raise NotFound(key)
        review = Review(
            osm_key=key,
            osm_version=base.osm_version,
            status=status,
            note=note or None,
            overrides=overrides,
        )
        self.store.save(review)
        self.reviews[key] = review
        return self.detail(key)

    def reset_review(self, key: str) -> dict[str, Any]:
        if key not in self.base:
            raise NotFound(key)
        self.store.delete(key)
        self.reviews.pop(key, None)
        return self.detail(key)

    # ----------------------------------------------------------------- resumo

    def summary(self) -> dict[str, Any]:
        buildings = self.all_buildings()
        rows = [self._row(b) for b in buildings]
        report = evaluate(buildings, self.policy.min_shape_confidence)
        selected, records = plan_export(
            buildings,
            self.policy,
            include_pending=self.cfg.export.include_pending,
            overwrite=self.cfg.export.overwrite_existing_tags,
        )
        ev = report.shapes
        labels = sorted({r for r, _ in ev.confusion} | {p for _, p in ev.confusion})
        return {
            "area": self.cfg.area.name,
            "source": str(self.source),
            "n_buildings": len(buildings),
            "status": dict(Counter(r["status"] for r in rows)),
            "needs_review": sum(r["needs_review"] for r in rows),
            "edited": sum(r["edited"] for r in rows),
            "shapes": dict(Counter(r["shape"] or "sem dados" for r in rows)),
            "roof_colours": dict(Counter(r["roof_colour"] for r in rows)),
            "osm3d": sum(r["osm3d"] for r in rows),
            "changes": dict(Counter(r["change"] for r in rows)),
            "changes_osm3d": dict(Counter(r["change"] for r in rows if r["osm3d"])),
            "palette": [{"label": c.label, "hex": c.hex} for c in PALETTE.values()],
            "default_colours": {"roof": DEFAULT_ROOF_HEX, "walls": DEFAULT_FACADE_HEX},
            "flags": dict(Counter(f for r in rows for f in r["flags"])),
            "evaluation": {
                "n_reference": ev.n_reference,
                "n_predicted": ev.n_predicted,
                "n_correct": ev.n_correct,
                "accuracy": _num(ev.accuracy, 3) if ev.accuracy is not None else None,
                "n_compatible": ev.n_compatible,
                "accuracy_compatible": _num(ev.accuracy_compatible, 3)
                if ev.accuracy_compatible is not None
                else None,
                "coverage": _num(ev.coverage, 3) if ev.coverage is not None else None,
                "labels": labels,
                "confusion": [[ev.confusion[(r, p)] for p in labels] for r in labels],
                "heights": {k: _num(v, 3) for k, v in report.heights.summary().items()},
            },
            "export": {
                "selected": len(selected),
                "conflicts": sum(bool(r.conflicts) for r in records),
                "reasons": dict(Counter(r.reason.split(":")[0] for r in records)),
            },
        }

    def export(self) -> dict[str, Any]:
        path, selected, records = export_area(self.cfg, self.all_buildings())
        return {
            "path": str(Path(path).resolve()),
            "exported": selected,
            "conflicts": sum(bool(r.conflicts) for r in records),
        }
