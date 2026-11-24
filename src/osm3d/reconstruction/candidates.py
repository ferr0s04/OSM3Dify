"""Edifícios em falta no OSM, a partir da cartografia municipal (geoportal SIG).

Cada polígono de edifício da cartografia municipal sem contorno no OSM passa a
candidato com o contorno municipal, se o **LiDAR o confirma** (pontos de classe 6 a
cobrir a área e altura acima do terreno). A cartografia é anterior ao LiDAR: um
polígono sem LiDAR é um edifício demolido ou escondido (sob árvores) e fica de fora.

Não há deteção de edifícios só pelo LiDAR/ortofoto: os contornos assim obtidos eram
maus e a maioria das câmaras municipais tem um portal SIG com a cartografia.

Os candidatos seguem depois o mesmo ajuste de telhado (LiDAR) e de cor dos edifícios
OSM e entram na revisão como ``new/<id>``. Só os aceites são exportados, como ways novas.
"""

from __future__ import annotations

import atexit
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import shapely
from pyproj import Transformer
from shapely.geometry import LineString, Point, Polygon
from shapely.geometry.base import BaseGeometry
from shapely.geometry.polygon import orient
from shapely.ops import polygonize, unary_union

from osm3d.acquisition.municipal import MunicipalBuilding
from osm3d.config import MunicipalSourceConfig
from osm3d.domain.models import Building, QAFlag
from osm3d.geometry import remove_spikes
from osm3d.geospatial.point_clouds import PointSet, read_points
from osm3d.geospatial.raster import DtmMosaic, sample_ground_elevation
from osm3d.geospatial.registration import estimate_tile_offset
from osm3d.geospatial.tiling import TileKey
from osm3d.parallel import ordered_map, process_pool, worker_count

log = logging.getLogger(__name__)

TILE_MARGIN_M = 30.0
MAX_SHIFT_M = 3.0  # desvio máximo cartografia municipal → LiDAR procurado por quadrícula
MIN_SHIFT_GAIN = 0.02  # ganho de IoU mínimo para usar o desvio (senão, nenhum)


# ----------------------------------------------------------------- contexto OSM


def _polygon_from_members(members: list[dict]) -> BaseGeometry | None:  # type: ignore[type-arg]
    outer = [
        LineString([(p["lon"], p["lat"]) for p in m["geometry"]])
        for m in members
        if m.get("type") == "way" and m.get("geometry") and m.get("role", "outer") != "inner"
    ]
    inner = [
        LineString([(p["lon"], p["lat"]) for p in m["geometry"]])
        for m in members
        if m.get("type") == "way" and m.get("geometry") and m.get("role") == "inner"
    ]
    polys = list(polygonize(unary_union(outer))) if outer else []
    if not polys:
        return None
    geom = unary_union(polys)
    if inner:
        geom = geom.difference(unary_union(list(polygonize(unary_union(inner)))))
    return geom


def load_context(
    path: Path, crs: str, relation_id: int | None
) -> tuple[list[BaseGeometry], BaseGeometry | None]:
    """(contornos OSM de toda a área das quadrículas, limite da área de estudo)."""
    data = json.loads(path.read_text(encoding="utf-8"))
    to_crs = Transformer.from_crs("EPSG:4326", crs, always_xy=True)

    def project(g: BaseGeometry) -> BaseGeometry:
        return shapely.transform(g, lambda c: np.column_stack(to_crs.transform(c[:, 0], c[:, 1])))

    buildings: list[BaseGeometry] = []
    boundary: BaseGeometry | None = None
    for el in data["elements"]:
        if (
            el["type"] == "way"
            and "building" in el.get("tags", {})
            and len(el.get("geometry", [])) >= 4
        ):
            coords = [(p["lon"], p["lat"]) for p in el["geometry"]]
            if coords[0] == coords[-1]:
                buildings.append(project(Polygon(coords)))
        elif el["type"] == "relation":
            geom = _polygon_from_members(el.get("members", []))
            if geom is None:
                continue
            if relation_id is not None and el["id"] == relation_id:
                boundary = project(geom)
            elif "building" in el.get("tags", {}):
                buildings.append(project(geom))
    return [shapely.make_valid(b) for b in buildings], boundary


def candidate_id(x: float, y: float) -> int:
    """Id estável (independente da ordem de deteção): posição do centróide, a 1 m."""
    return (int(round(x)) + 500_000) * 1_000_000 + int(round(y))


# ------------------------------------------------------------------- deteção


def detect_candidates(
    lidar_tiles: dict[str, Path],
    dtm_tiles: dict[str, Path],
    osm_buildings: list[BaseGeometry],
    boundary: BaseGeometry | None,
    municipal: list[MunicipalBuilding],
    municipal_cfg: MunicipalSourceConfig,
    progress: Callable[[str], None] | None = None,
    workers: int | None = None,
) -> list[Building]:
    """Edifícios municipais em falta no OSM e confirmados pelo LiDAR, quadrícula a
    quadrícula (``workers``: processos)."""
    available = sorted(set(lidar_tiles) & set(dtm_tiles))
    osm_index = shapely.STRtree(osm_buildings) if osm_buildings else None
    muni_index = shapely.STRtree([m.geometry for m in municipal])
    tasks: list[_TileTask] = []
    for tile in available:
        area = shapely.box(*_tile_bounds(tile))
        near_m = [municipal[i] for i in muni_index.query(area)]
        if not near_m:
            continue
        near = [osm_buildings[i] for i in osm_index.query(area)] if osm_index else []
        tasks.append((tile, near, near_m))
    env = _TileEnv(
        {t: lidar_tiles[t] for t in available},
        [dtm_tiles[t] for t in available],
        boundary,
        municipal_cfg,
    )
    # Uma quadrícula por processo: o resultado é o mesmo, pela mesma ordem.
    found: dict[int, Building] = {}
    with (
        _OpenedEnv(env) as here,
        process_pool(
            worker_count(workers, len(tasks), min_tasks=2), _init_tile_worker, (env,)
        ) as pool,
    ):
        for tile_json, message in ordered_map(
            pool, _tile_task, tasks, lambda t: _detect_tile(t, here)
        ):
            for b in (Building.model_validate_json(j) for j in tile_json):
                found[b.osm_id] = b
            if progress and message:
                progress(message)
    log.info("%d edifícios municipais em falta no OSM", len(found))
    return list(found.values())


def _tile_bounds(tile: str) -> tuple[float, float, float, float]:
    x0, y0, x1, y1 = TileKey.from_dgt_name(tile).bounds
    m = TILE_MARGIN_M
    return (x0 - m, y0 - m, x1 + m, y1 + m)


@dataclass(frozen=True)
class _TileEnv:
    """O que a deteção de uma quadrícula precisa (passa para os processos do pool)."""

    lidar: dict[str, Path]
    dtm: list[Path]
    boundary: BaseGeometry | None
    municipal_cfg: MunicipalSourceConfig


class _OpenedEnv:
    """``_TileEnv`` com o MDT aberto (em cada processo)."""

    def __init__(self, env: _TileEnv) -> None:
        self.env = env
        self.dtm = DtmMosaic(env.dtm)

    def close(self) -> None:
        self.dtm.close()

    def __enter__(self) -> _OpenedEnv:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


# (quadrícula, contornos OSM perto, cartografia municipal perto)
_TileTask = tuple[str, list[BaseGeometry], list[MunicipalBuilding]]
_worker_env: _OpenedEnv | None = None


def _init_tile_worker(env: _TileEnv) -> None:
    global _worker_env
    _worker_env = _OpenedEnv(env)
    atexit.register(_worker_env.close)


def _tile_task(task: _TileTask) -> tuple[list[str], str]:
    assert _worker_env is not None
    return _detect_tile(task, _worker_env)


def _detect_tile(task: _TileTask, opened: _OpenedEnv) -> tuple[list[str], str]:
    """Candidatos de uma quadrícula (em JSON, para passar entre processos) e o resumo."""
    tile, near, near_m = task
    env = opened.env
    bounds = _tile_bounds(tile)
    pts = read_points(env.lidar.values(), bounds, [6])
    if not len(pts):
        return [], ""
    ctx = _TileContext(
        tile, TileKey.from_dgt_name(tile).bounds, bounds, pts, opened.dtm, near, env.boundary
    )
    offset = municipal_offset(ctx, near_m)
    new, stats = municipal_candidates(ctx, near_m, offset, env.municipal_cfg)
    message = (
        f"  quadrícula {tile}: {stats.from_municipal} contornos municipais em falta no OSM"
        f" confirmados pelo LiDAR, {stats.without_lidar} sem LiDAR (demolidos ou sob"
        f" árvores); desvio ({offset[0]:+.1f}, {offset[1]:+.1f}) m"
    )
    return [b.model_dump_json() for b in new], message


@dataclass
class _TileContext:
    tile: str
    core: tuple[float, float, float, float]  # a quadrícula (dono do candidato)
    bounds: tuple[float, float, float, float]  # com margem
    pts: PointSet  # classe 6, no referencial do LiDAR
    dtm: DtmMosaic
    osm: list[BaseGeometry]  # contornos OSM perto
    boundary: BaseGeometry | None

    def owns(self, c: Point) -> bool:
        """Cada candidato é tratado na quadrícula do seu centróide (sem duplicados)."""
        x0, y0, x1, y1 = self.core
        inside = x0 <= c.x < x1 and y0 <= c.y < y1
        return inside and (self.boundary is None or self.boundary.contains(c))


# ------------------------------------------------------- cartografia municipal

MUNICIPAL_INSET_M = 0.5  # erosão antes de medir o LiDAR (desvio residual, beirais)
MUNICIPAL_CELL_M = 1.0
MUNICIPAL_MIN_COVER = 0.5  # fração da área com pontos de edifício
MUNICIPAL_MIN_HEIGHT_M = 1.8  # telheiros e anexos baixos


@dataclass
class MunicipalStats:
    from_municipal: int = 0
    without_lidar: int = 0


def municipal_offset(ctx: _TileContext, municipal: list[MunicipalBuilding]) -> tuple[float, float]:
    """Desvio cartografia municipal → LiDAR da quadrícula (só para medir o LiDAR)."""
    polys = [m.geometry for m in municipal if m.building_tag is not None]
    offset, iou0, iou1 = estimate_tile_offset(polys, ctx.pts, ctx.bounds, max_shift_m=MAX_SHIFT_M)
    return offset if iou1 - iou0 >= MIN_SHIFT_GAIN else (0.0, 0.0)


def lidar_support(footprint_lidar: Polygon, ctx: _TileContext) -> tuple[float, float | None]:
    """(fração da área com pontos de edifício, altura P90 acima do terreno).

    Mede-se no contorno erodido ``MUNICIPAL_INSET_M``, em células de 1 m.
    """
    inner = footprint_lidar.buffer(-MUNICIPAL_INSET_M, join_style="mitre")
    if inner.is_empty or inner.area < 2.0:
        inner = footprint_lidar
    xyz = ctx.pts.xyz
    bx0, by0, bx1, by1 = inner.bounds
    near = np.flatnonzero(
        (xyz[:, 0] >= bx0) & (xyz[:, 0] <= bx1) & (xyz[:, 1] >= by0) & (xyz[:, 1] <= by1)
    )
    inside = near[shapely.contains_xy(inner, xyz[near, 0], xyz[near, 1])]
    if not len(inside):
        return 0.0, None
    cells = np.unique(np.floor(xyz[inside, :2] / MUNICIPAL_CELL_M).astype(np.int64), axis=0)
    cover = min(1.0, len(cells) * MUNICIPAL_CELL_M**2 / inner.area)
    ground = sample_ground_elevation(ctx.dtm, footprint_lidar, 2.0, 5)
    if ground is None:
        return cover, None
    return cover, float(np.percentile(xyz[inside, 2], 90)) - ground


def municipal_candidate(
    m: MunicipalBuilding,
    ctx: _TileContext,
    offset: tuple[float, float],
    cfg: MunicipalSourceConfig,
) -> Building | None | bool:
    """Candidato com o contorno municipal, se o LiDAR confirma o edifício.

    ``None``: não é candidato (tipo excluído, pequeno, já no OSM, fora da quadrícula);
    ``False``: o LiDAR não o confirma. A cartografia é anterior ao LiDAR, por isso é
    um edifício demolido ou escondido (sob árvores).
    """
    tag = m.building_tag
    if tag is None or not ctx.owns(m.geometry.centroid):
        return None
    footprint = orient(remove_spikes(Polygon(m.geometry.exterior)), 1.0)
    if footprint.area < cfg.min_area_m2:
        return None
    if any(footprint.intersection(g).area > 0.3 * footprint.area for g in ctx.osm):
        return None
    dx, dy = offset
    in_lidar = shapely.transform(footprint, lambda c: c + np.array([dx, dy]))
    cover, height = lidar_support(in_lidar, ctx)
    if height is None or cover < MUNICIPAL_MIN_COVER or height < MUNICIPAL_MIN_HEIGHT_M:
        return False
    flags = [
        QAFlag(
            code="missing_in_osm",
            message=f"Edifício da cartografia municipal sem contorno no OSM: {m.descr} "
            f"({footprint.area:.0f} m², ~{height:.1f} m de altura). Contorno municipal "
            f"(gid {m.gid}), confirmado pelo LiDAR em {cover:.0%} da área.",
            severity="info",
        )
    ]
    if m.geometry.interiors:
        flags.append(
            QAFlag(
                code="municipal_courtyard",
                message="O contorno municipal tem pátio interior, que não foi incluído: "
                "rever (multipolígono).",
                severity="warning",
            )
        )
    c = footprint.centroid
    return Building(
        osm_type="new",
        osm_id=candidate_id(c.x, c.y),
        osm_version=0,
        footprint=footprint,
        existing_tags={"building": tag},
        qa_flags=flags,
        diagnostics={
            "municipal_offset_dx": dx,
            "municipal_offset_dy": dy,
            "municipal_gid": float(m.gid),
            "municipal_lidar_cover": cover,
        },
    )


def municipal_candidates(
    ctx: _TileContext,
    municipal: list[MunicipalBuilding],
    offset: tuple[float, float],
    cfg: MunicipalSourceConfig,
) -> tuple[list[Building], MunicipalStats]:
    """Candidatos da quadrícula: cada polígono municipal (tipos de edifício) sem
    contorno no OSM e **confirmado pelo LiDAR**, com o contorno municipal (que separa
    casa, anexo, garagem e telheiro, mesmo encostados)."""
    stats = MunicipalStats()
    out: list[Building] = []
    for m in municipal:
        cand = municipal_candidate(m, ctx, offset, cfg)
        if cand is False:
            stats.without_lidar += 1
        elif isinstance(cand, Building):
            out.append(cand)
    stats.from_municipal = len(out)
    return out, stats
