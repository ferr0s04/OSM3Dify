"""Lógica da aplicação de revisão, independente do framework web."""

from __future__ import annotations

import math
from collections import Counter
from pathlib import Path
from typing import Any

import numpy as np
import shapely
from PIL import ImageColor
from pydantic import BaseModel, Field, model_validator
from pyproj import Transformer
from shapely.geometry import mapping

from osm3d.app.points import BUILDING_CLASS, TilePointCache
from osm3d.appearance.roof_colours import DEFAULT_FACADE_HEX, DEFAULT_ROOF_HEX, PALETTE
from osm3d.config import Config
from osm3d.domain.models import Building, BuildingPart, PartsLayout, ReviewStatus, RoofShape
from osm3d.evaluation.benchmark import evaluate, normalize_osm_shape
from osm3d.export.geopackage import read_buildings
from osm3d.export.mesh import Mesh, roof_mesh, wall_mesh
from osm3d.export.osm_changes import ChangeSummary, classify_change
from osm3d.export.osm_xml import has_parts, outline_tags, part_way_tags, plan_export
from osm3d.export.s3db_tags import TagMerge, merge_tags
from osm3d.geospatial.point_clouds import index_tiles
from osm3d.parts import layout_from_polygons, merge_parts, split_part, validate_layout
from osm3d.pipeline import (
    Stage,
    area_dir,
    artefact_path,
    context_path,
    export_area,
    ortho_files,
    refresh_context,
    review_store_path,
    tag_policy,
)
from osm3d.reconstruction.building import fit_params
from osm3d.reconstruction.ground import STEP_THRESHOLD_M
from osm3d.reconstruction.ortho_outline import OrthoMosaic
from osm3d.reconstruction.parts import fit_layout
from osm3d.reconstruction.skeleton import has_skeleton, renders_as_skeleton
from osm3d.review import (
    BUILDING_TYPES,
    Overrides,
    Review,
    ReviewStore,
    apply_corrections,
    apply_parts,
    apply_review,
    orientation_of,
)
from osm3d.validation.topology import (
    CODES as TOPOLOGY_CODES,
)
from osm3d.validation.topology import (
    Surroundings,
    TopologyRules,
    blocks_buildings,
    default_mode,
    load_surroundings,
    mode_of,
    resolve_topology,
)

MAX_BUILDING_POINTS = 40_000
MAX_EDITOR_POINTS = 6_000


class LayoutIn(BaseModel):
    """Divisão em partes em coordenadas locais do editor."""

    vertices: list[tuple[float, float]]
    parts: list[list[int]]

    @model_validator(mode="after")
    def _check_indices(self) -> LayoutIn:
        n = len(self.vertices)
        for ring in self.parts:
            if len(ring) < 3 or any(not 0 <= k < n for k in ring):
                raise ValueError("Anel de parte inválido (menos de 3 vértices ou índice fora)")
        return self


class PartsIn(BaseModel):
    layout: LayoutIn
    overrides: list[Overrides] = Field(default_factory=list)


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
        # Edifícios em falta no OSM (fase candidates, opcional): revistos como os outros,
        # com a chave "new/<id>".
        candidates = artefact_path(cfg, Stage.CANDIDATES)
        if candidates.exists():
            self.base |= {b.key: b for b in read_buildings(candidates)}
        self.store = ReviewStore(review_store_path(cfg))
        self.reviews: dict[str, Review] = self.store.all()
        self.points = TilePointCache(
            index_tiles(cfg.sources.lidar_dir, (".laz", ".las")), area_dir(cfg) / "cache"
        )
        self.policy = tag_policy(cfg)
        self._to_wgs84 = Transformer.from_crs(cfg.processing_crs, "EPSG:4326", always_xy=True)
        self._to_3857 = Transformer.from_crs(cfg.processing_crs, "EPSG:3857", always_xy=True)
        self._scene_origin: tuple[float, float] | None = None
        self._scene_cache: dict[str, dict[str, Any] | None] = {}
        self._wgs84: dict[str, Any] = {}
        # Contexto OSM (landuse, vias, muros, edifícios) e a topologia dos edifícios novos
        # face a ele, com o modo escolhido na revisão; refeita quando a revisão muda.
        self._env: Surroundings | None = None
        self._env_mtime: float | None = None
        self._topo: dict[str, Building] | None = None
        self._ctx_geojson: tuple[float, dict[str, Any]] | None = None

    def refresh_points(self) -> None:
        """Volta a indexar os LAZ (ficheiros raw descarregados ou apagados)."""
        self.points.set_tiles(index_tiles(self.cfg.sources.lidar_dir, (".laz", ".las")))

    # ------------------------------------------------------------------ básico

    def _h(self, v: float) -> float:
        """Altura arredondada como na exportação (0,5 m por omissão)."""
        return round_to(v, self.cfg.export.height_rounding_m)

    def _rh(self, v: float) -> float:
        return round_to(v, self.cfg.export.roof_height_rounding_m)

    def building(self, key: str) -> Building:
        if key not in self.base:
            raise NotFound(key)
        if self.base[key].osm_type == "new" and (hit := self._topology().get(key)) is not None:
            return hit
        return apply_review(self.base[key], self.reviews.get(key))

    # ---------------------------------------------------------- topologia/OSM

    def _surroundings(self) -> Surroundings | None:
        path = context_path(self.cfg)
        if not path.exists():
            return None
        mtime = path.stat().st_mtime
        if self._env is None or self._env_mtime != mtime:
            fetched = artefact_path(self.cfg, Stage.FETCH)
            self._env = load_surroundings(path, self.cfg.processing_crs, fetched)
            self._env_mtime = mtime
            self._topo = None
        return self._env

    def _topology(self) -> dict[str, Building]:
        """Edifícios novos (com a revisão) depois dos recortes e com as sinalizações.

        Os rejeitados e os arquivados não entram (não vão ser exportados).
        """
        env = self._surroundings()
        if self._topo is None:
            active = [
                b
                for k, base in self.base.items()
                if base.osm_type == "new"
                and not (b := apply_review(base, self.reviews.get(k))).review_archived
                and b.review_status != "rejected"
            ]
            if env is not None:
                rules = TopologyRules.from_config(self.cfg.topology)
                active = resolve_topology(active, env, rules)[0]
            self._topo = {b.key: b for b in active}
            self._wgs84 = {k: v for k, v in self._wgs84.items() if not k.startswith("new/")}
        return self._topo

    def _topology_changed(self) -> None:
        self._topo = None

    def osm_context(self) -> dict[str, Any]:
        """Landuse, vias, muros/vedações e edifícios do OSM à volta da área (GeoJSON)."""
        path = context_path(self.cfg)
        if not path.exists():
            return {
                "type": "FeatureCollection",
                "features": [],
                "date": None,
                "outdated": True,
                "missing": ["contexto OSM (correr a fase candidates ou atualizar)"],
            }
        mtime = path.stat().st_mtime
        if self._ctx_geojson is None or self._ctx_geojson[0] != mtime:
            self._ctx_geojson = (mtime, osm_context_geojson(path))
        return self._ctx_geojson[1]

    def refresh_osm_context(self) -> dict[str, Any]:
        """Volta a pedir o contexto OSM ao Overpass (p.ex. depois de editar no OSM)."""
        refresh_context(self.cfg, force=True)
        self._topology_changed()
        ctx = self.osm_context()
        return {"date": ctx.get("date"), "n": len(ctx["features"])}

    def all_buildings(self) -> list[Building]:
        return [self.building(k) for k in self.base]

    def _geom_wgs84(self, key: str) -> Any:
        if key not in self._wgs84:
            # Edifícios novos: o contorno que vai ser exportado (recortes incluídos).
            g = (self.building(key) if key.startswith("new/") else self.base[key]).footprint
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
            "candidate": b.osm_type == "new",  # em falta no OSM, detetado no LiDAR
            # Origem do contorno do candidato: cartografia municipal ou LiDAR/ortofoto.
            "candidate_source": (
                None
                if b.osm_type != "new"
                else "municipal"
                if "municipal_gid" in b.diagnostics
                else "lidar"
            ),
            "building_type": b.existing_tags.get("building"),
            "status": b.review_status,
            "archived": b.review_archived,
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
                            "archived",
                            "candidate",
                            "candidate_source",
                            "building_type",
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
        proposed = outline_tags(b, self.policy)
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
            # Com partes: tags de cada building:part (novas ways).
            "parts": [part_way_tags(p, self.policy) for p in b.parts] if has_parts(b) else [],
        }

    def _part_colours(self, b: Building, part: BuildingPart) -> dict[str, str]:
        """Cores de uma building:part (tags da própria parte, senão por omissão)."""
        roof = part.roof.colour.value if part.roof.colour else None
        walls = part.facade_colour.value if part.facade_colour else None
        walls = walls or b.existing_tags.get("building:colour")
        return {
            "roof": display_colour(roof, DEFAULT_ROOF_HEX) if roof else DEFAULT_ROOF_HEX,
            "walls": display_colour(walls, DEFAULT_FACADE_HEX) if walls else DEFAULT_FACADE_HEX,
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
                if est is not None and est.source.value != "manual":
                    out[f"{name}_via"] = est.source.value  # orthophoto | lidar
        return out

    def _part_info(self, b: Building, index: int = 0) -> dict[str, Any] | None:
        if len(b.parts) <= index:
            return None
        p = b.parts[index]
        r = p.roof
        return {
            "min_height": self._h(p.min_height) if p.min_height else 0.0,
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
            "osm_url": (
                None
                if b.osm_type == "new"
                else f"https://www.openstreetmap.org/{b.osm_type}/{b.osm_id}"
            ),
            "note": b.review_note,
            "overrides": review.overrides.model_dump() if review else Overrides().model_dump(),
            "reviewed_at": review.reviewed_at.isoformat() if review else None,
            "qa_flags": [f.model_dump() for f in b.qa_flags],
            "diagnostics": {k: _num(v, 3) for k, v in b.diagnostics.items()},
            "existing_tags": b.existing_tags,
            # Tipo estimado (antes da correção da revisão), p.ex. o da cartografia municipal.
            "estimated_building": base.existing_tags.get("building"),
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
            "ground": self._ground_info(b),
            "has_layout": b.layout is not None,
            "has_suggestion": base.suggested_layout is not None,
            "parts_editable": self._editable(b) is None,
            "n_parts": len(b.parts),
            "offset": list(b.registration_offset_m),
            "bbox": list(self._geom_wgs84(key).bounds),
            # Sobreposições com o OSM: o modo em uso e o padrão da origem do contorno.
            "topology": (
                None
                if b.osm_type != "new"
                else {
                    "mode": mode_of(b),
                    "default": default_mode(base),
                    "chosen": b.topology_mode,
                    "relevant": any(f.code in TOPOLOGY_CODES for f in b.qa_flags),
                    "trimmed_m2": _num(base.footprint.area - b.footprint.area, 1),
                }
            ),
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
        ground = b.ground_elevation_m
        multi = has_parts(b)
        dx, dy = b.registration_offset_m
        meshes = []
        for p in b.parts:
            part_fp = (
                shapely.transform(p.geometry, lambda c: c + np.array([dx, dy])) if multi else fp
            )
            colours = self._part_colours(b, p) if multi else out["colours"]
            meshes.append(self._part_mesh(part_fp, p, ground, origin) | {"colours": colours})
        out["parts"] = meshes
        out["roof"], out["walls"] = meshes[0]["roof"], meshes[0]["walls"]
        return out

    def _part_mesh(
        self, fp: Any, p: BuildingPart, ground: float, origin: tuple[float, float, float]
    ) -> dict[str, Any]:
        roof, walls = self._part_meshes(fp, p, ground)
        rings = [(np.asarray(fp.exterior.coords)[:, :2] - origin[:2]).round(2).tolist()]
        return {"roof": roof.to_json(origin), "walls": walls.to_json(origin), "outline": rings}

    def _part_meshes(
        self, fp: Any, p: BuildingPart, ground: float, coarse: bool = False
    ) -> tuple[Mesh, Mesh]:
        """Telhado e paredes de uma parte, como os renderizadores os vão desenhar."""
        shape = p.roof.shape.value
        # O modelo mostrado usa as alturas arredondadas que vão ser exportadas.
        height = self._h(p.height.value)
        base = ground + (self._h(p.min_height) if p.min_height else 0.0)
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
        skeleton = model is not None and model.skeleton
        resolution = (0.5 if skeleton else 1.0) if coarse else (0.25 if skeleton else 0.5)
        return (
            roof_mesh(fp, model, shape, flat_z, resolution),
            wall_mesh(fp, model, shape, base, flat_z, resolution),
        )

    # ---------------------------------------------------------- cena do mapa

    def scene_building(self, b: Building) -> dict[str, Any] | None:
        """Malhas de um edifício para o modo 3D do mapa (EPSG:3857 relativo à origem).

        x/y em metros Web Mercator relativos a ``scene_origin``; z em metros reais acima
        do terreno do edifício. Telhados com a forma e as cores que vão ser aplicadas.
        """
        if not b.parts or b.ground_elevation_m is None:
            return None
        ground = b.ground_elevation_m
        dx, dy = b.registration_offset_m
        multi = has_parts(b)
        out = []
        for p in b.parts:
            geom = p.geometry if multi else b.footprint
            # Na cena, o edifício fica sobre o contorno OSM (sem o deslocamento do LiDAR).
            shifted = shapely.transform(geom, lambda c: c + np.array([dx, dy]))
            roof, walls = self._part_meshes(shifted, p, ground, coarse=True)
            colours = self._part_colours(b, p) if multi else self._colours(b)
            out.append(
                {
                    "roof": self._scene_mesh(roof, ground, (dx, dy)),
                    "walls": self._scene_mesh(walls, ground, (dx, dy)),
                    "roof_colour": colours["roof"],
                    "walls_colour": colours["walls"],
                }
            )
        return {"key": b.key, "parts": out}

    def _scene_mesh(self, m: Mesh, ground: float, shift: tuple[float, float]) -> dict[str, Any]:
        if not len(m.vertices):
            return {"v": [], "f": []}
        x = m.vertices[:, 0] - shift[0]
        y = m.vertices[:, 1] - shift[1]
        mx, my = self._to_3857.transform(x, y)
        ox, oy = self.scene_origin_3857
        v = np.column_stack([np.asarray(mx) - ox, np.asarray(my) - oy, m.vertices[:, 2] - ground])
        return {"v": np.round(v, 2).ravel().tolist(), "f": m.faces.ravel().tolist()}

    @property
    def scene_origin_3857(self) -> tuple[float, float]:
        if self._scene_origin is None:
            c = shapely.union_all([b.footprint for b in self.base.values()]).centroid
            x, y = self._to_3857.transform(c.x, c.y)
            self._scene_origin = (float(x), float(y))
        return self._scene_origin

    def scene(self) -> dict[str, Any]:
        """Cena 3D de toda a área (em cache; invalidada por edifício nas revisões)."""
        buildings = []
        for key in self.base:
            if key not in self._scene_cache:
                self._scene_cache[key] = self.scene_building(self.building(key))
            if self._scene_cache[key] is not None:
                buildings.append(self._scene_cache[key])
        ox, oy = self.scene_origin_3857
        lon, lat = Transformer.from_crs("EPSG:3857", "EPSG:4326", always_xy=True).transform(ox, oy)
        return {"origin": [lon, lat], "buildings": buildings}

    def scene_one(self, key: str) -> dict[str, Any] | None:
        self._scene_cache.pop(key, None)
        self._scene_cache[key] = self.scene_building(self.building(key))
        return self._scene_cache[key]

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
        b = apply_corrections(base, overrides)
        return {
            "mesh": self.mesh_for(b),
            "tags": self._tags(b),
            "effective": self._part_info(b),
            "ground": self._ground_info(b),
        }

    def _ground_info(self, b: Building) -> dict[str, Any] | None:
        """Terreno de referência e, em encosta, a altura com cada candidato."""
        g = b.ground
        if g is None or not b.parts or b.ground_elevation_m is None:
            return None
        top = b.ground_elevation_m + b.parts[0].height.value
        return {
            "low_m": _num(g.low_m),
            "high_m": _num(g.high_m),
            "step_m": _num(g.step_m),
            "choice": g.choice,
            "basis": g.basis,
            "reason": g.reason,
            "is_slope": g.step_m > STEP_THRESHOLD_M,
            "heights": {c: self._h(top - g.elevation(c)) for c in ("low", "high")},
        }

    # ------------------------------------------------------------------ partes
    #
    # O editor trabalha em coordenadas locais (metros, referencial do OSM) relativas ao
    # centróide do contorno: x = este, y = norte.

    def _parts_origin(self, b: Building) -> tuple[float, float]:
        c = b.footprint.centroid
        return (c.x, c.y)

    def _to_local(self, layout: PartsLayout, origin: tuple[float, float]) -> dict[str, Any]:
        ox, oy = origin
        return {
            "vertices": [[round(x - ox, 3), round(y - oy, 3)] for x, y in layout.vertices],
            "parts": layout.parts,
            "source": layout.source,
        }

    def _from_local(self, data: LayoutIn, origin: tuple[float, float]) -> PartsLayout:
        ox, oy = origin
        return PartsLayout(
            vertices=[(x + ox, y + oy) for x, y in data.vertices], parts=data.parts, source="manual"
        )

    def _editable(self, b: Building) -> str | None:
        if b.osm_type not in ("way", "new"):
            return "Divisão em partes só para contornos simples (way), não multipolígonos"
        if b.osm_parts:
            return "O edifício já tem building:part no OSM: editar no JOSM"
        if b.ground_elevation_m is None:
            return "Sem cota do terreno"
        return None

    def parts_view(self, key: str) -> dict[str, Any]:
        """Dados do editor de partes: contorno, pontos LiDAR, divisão atual e sugerida."""
        b = self.building(key)
        origin = self._parts_origin(b)
        ox, oy = origin
        outline = [
            [round(x - ox, 3), round(y - oy, 3)] for x, y in b.footprint.exterior.coords[:-1]
        ]
        dx, dy = b.registration_offset_m
        minx, miny, maxx, maxy = self._shifted(b).bounds
        ps = self.points.around((minx - 2, miny - 2, maxx + 2, maxy + 2))
        pts: list[list[float]] = []
        if len(ps):
            keep = ps.classification == BUILDING_CLASS
            xyz = ps.xyz[keep]
            if len(xyz) > MAX_EDITOR_POINTS:
                xyz = xyz[
                    np.random.default_rng(0).choice(len(xyz), MAX_EDITOR_POINTS, replace=False)
                ]
            ground = b.ground_elevation_m or float(xyz[:, 2].min(initial=0))
            local = np.column_stack([xyz[:, 0] - dx - ox, xyz[:, 1] - dy - oy, xyz[:, 2] - ground])
            pts = np.round(local, 2).tolist()
        review = self.reviews.get(key)
        return {
            "origin": list(origin),
            "outline": outline,
            "points": pts,
            "layout": self._to_local(b.layout, origin) if b.layout else None,
            "suggested": (
                self._to_local(b.suggested_layout, origin) if b.suggested_layout else None
            ),
            "overrides": [o.model_dump() for o in review.part_overrides] if review else [],
            "editable": self._editable(b) is None,
            "not_editable_reason": self._editable(b),
        }

    def _fit(self, b: Building, layout: PartsLayout) -> PartsLayout:
        minx, miny, maxx, maxy = self._shifted(b).bounds
        points = self.points.around((minx - 1, miny - 1, maxx + 1, maxy + 1))
        orthos = ortho_files(self.cfg)
        if not orthos:
            return fit_layout(b, layout, points, fit_params(self.cfg))
        # Aberto por pedido: os datasets do rasterio não são partilháveis entre threads.
        with OrthoMosaic(orthos) as ortho:
            return fit_layout(b, layout, points, fit_params(self.cfg), ortho)

    def parts_preview(self, key: str, data: LayoutIn, overrides: list[Overrides]) -> dict[str, Any]:
        """Ajusta cada parte da divisão editada e devolve tags, malha 3D e problemas."""
        b = self.building(key)
        if (why := self._editable(b)) is not None:
            raise ValueError(why)
        layout = self._from_local(data, self._parts_origin(b))
        problems = validate_layout(layout, b.footprint)
        if any("inválido" in p or "nula" in p for p in problems):
            return {"problems": problems, "parts": [], "mesh": None, "tags": None}
        fitted = self._fit(b, layout)
        built = apply_parts(b, fitted, overrides)
        return {
            "problems": problems,
            "parts": [
                (self._part_info(built, i) or {})
                | {"tags": part_way_tags(p, self.policy), "area": _num(p.geometry.area, 1)}
                for i, p in enumerate(built.parts)
            ],
            "mesh": self.mesh_for(built),
            "tags": self._tags(built),
        }

    def parts_split(
        self,
        key: str,
        data: LayoutIn,
        index: int,
        a: tuple[float, float],
        b_pt: tuple[float, float],
    ) -> dict[str, Any]:
        b = self.building(key)
        origin = self._parts_origin(b)
        layout = self._from_local(data, origin)
        if not 0 <= index < len(layout.parts):
            raise ValueError("Parte inexistente")
        ox, oy = origin
        new = split_part(layout, index, (a[0] + ox, a[1] + oy), (b_pt[0] + ox, b_pt[1] + oy))
        return {"layout": self._to_local(new, origin)}

    def parts_merge(self, key: str, data: LayoutIn, index: int) -> dict[str, Any]:
        b = self.building(key)
        origin = self._parts_origin(b)
        layout = self._from_local(data, origin)
        if not 0 <= index < len(layout.parts):
            raise ValueError("Parte inexistente")
        return {"layout": self._to_local(merge_parts(layout, index), origin)}

    def parts_start(self, key: str) -> dict[str, Any]:
        """Divisão inicial manual: o contorno inteiro como uma só parte (para dividir)."""
        b = self.building(key)
        if (why := self._editable(b)) is not None:
            raise ValueError(why)
        layout = layout_from_polygons([b.footprint], source="manual")
        return {"layout": self._to_local(layout, self._parts_origin(b))}

    def save_review(
        self,
        key: str,
        status: ReviewStatus,
        note: str | None,
        overrides: Overrides,
        parts: PartsIn | None = None,
    ) -> dict[str, Any]:
        base = self.base.get(key)
        if base is None:
            raise NotFound(key)
        layout, part_overrides = None, []
        if parts is not None:
            b = apply_corrections(base, overrides)
            if (why := self._editable(b)) is not None:
                raise ValueError(why)
            layout = self._from_local(parts.layout, self._parts_origin(b))
            problems = validate_layout(layout, b.footprint)
            if problems:
                raise ValueError("Divisão em partes inválida: " + "; ".join(problems))
            layout = self._fit(b, layout)
            part_overrides = parts.overrides
        review = Review(
            osm_key=key,
            osm_version=base.osm_version,
            status=status,
            note=note or None,
            overrides=overrides,
            parts=layout,
            part_overrides=part_overrides,
        )
        self.store.save(review)
        self.reviews[key] = review
        self._scene_cache.pop(key, None)
        self._topology_changed()
        return self.detail(key)

    def archive(self, status: ReviewStatus, archived: bool = True) -> dict[str, Any]:
        """(Des)arquiva todos os aceites ou todos os rejeitados."""
        if status == "pending":
            raise ValueError("Só se arquivam aceites ou rejeitados")
        changed = self.store.set_archived(status, archived)
        self.reviews = self.store.all()
        self._topology_changed()
        return {"status": status, "archived": archived, "changed": changed}

    def reset_review(self, key: str) -> dict[str, Any]:
        if key not in self.base:
            raise NotFound(key)
        self.store.delete(key)
        self.reviews.pop(key, None)
        self._scene_cache.pop(key, None)
        self._topology_changed()
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
            "status": dict(Counter(r["status"] for r in rows if not r["archived"])),
            "archived": dict(Counter(r["status"] for r in rows if r["archived"])),
            "needs_review": sum(r["needs_review"] for r in rows),
            "edited": sum(r["edited"] for r in rows),
            "shapes": dict(Counter(r["shape"] or "sem dados" for r in rows)),
            "roof_colours": dict(Counter(r["roof_colour"] for r in rows)),
            "osm3d": sum(r["osm3d"] for r in rows),
            "changes": dict(Counter(r["change"] for r in rows)),
            "changes_osm3d": dict(Counter(r["change"] for r in rows if r["osm3d"])),
            "palette": [{"label": c.label, "hex": c.hex} for c in PALETTE.values()],
            "building_types": [{"value": k, "label": v} for k, v in BUILDING_TYPES.items()],
            "candidate_sources": dict(
                Counter(r["candidate_source"] for r in rows if r["candidate_source"])
            ),
            "min_colour_confidence": self.policy.min_colour_confidence,
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
        path, selected, records, topo = export_area(self.cfg, self.all_buildings())
        return {
            "path": str(Path(path).resolve()),
            "exported": selected,
            "conflicts": sum(bool(r.conflicts) for r in records),
            # Edifícios novos: recortes, landuse/edifícios do OSM ajustados e conflitos que
            # ficam (pormenor no relatório CSV).
            "trimmed": topo.result.trimmed if topo else 0,
            "osm_adjusted": topo.ways_adjusted if topo else 0,
            "topology_conflicts": (topo.result.conflicts + topo.not_adjusted) if topo else 0,
            "topology": topo.summary() if topo else None,
        }


def _context_kind(tags: dict[str, str]) -> tuple[str, str] | None:
    """(camada, valor) de um elemento do contexto OSM no mapa."""
    for key in ("building", "landuse", "highway", "barrier"):
        if key in tags:
            return key, tags[key]
    return None


def osm_context_geojson(path: Path) -> dict[str, Any]:
    """GeoJSON (WGS84) do contexto OSM da fase candidates, para o mapa da revisão.

    ``kind``: building, landuse, highway ou barrier; ``value``: o valor da tag;
    ``crossing``: a via não pode atravessar edifícios (como nos validadores).
    """
    import json

    from shapely.geometry import LineString, Polygon

    from osm3d.validation.topology import _members_polygon

    data = json.loads(path.read_text(encoding="utf-8"))
    feats: list[dict[str, Any]] = []
    for el in data.get("elements", []):
        tags = el.get("tags", {})
        kv = _context_kind(tags)
        if kv is None:
            continue
        kind, value = kv
        geom: Any = None
        coords = [(p["lon"], p["lat"]) for p in el.get("geometry", []) if p]
        if el["type"] == "way" and len(coords) >= 2:
            closed = len(coords) >= 4 and coords[0] == coords[-1]
            area = kind in ("building", "landuse") or tags.get("area") == "yes"
            geom = Polygon(coords) if closed and area else LineString(coords)
        elif el["type"] == "relation" and tags.get("type") == "multipolygon":
            geom = _members_polygon(el.get("members", []))
        if geom is None or geom.is_empty:
            continue
        feats.append(
            {
                "type": "Feature",
                "geometry": mapping(geom),
                "properties": {
                    "kind": kind,
                    "value": value,
                    "id": f"{el['type']}/{el['id']}",
                    "name": tags.get("name"),
                    "crossing": kind == "highway" and blocks_buildings(tags),
                },
            }
        )
    from osm3d.acquisition.osm import CONTEXT_VERSION

    date = data.get("osm3s", {}).get("timestamp_osm_base")
    version = data.get("osm3d_context", 1)
    missing = []
    if version < 3:
        missing.append("muros e vedações (barrier)")
    if version < 2:
        missing.append("landuse e vias")
    return {
        "type": "FeatureCollection",
        "features": feats,
        "date": date,
        # Contexto obtido por uma versão anterior: faltam dados que agora se usam.
        "outdated": version != CONTEXT_VERSION,
        "missing": missing,
    }
