"""Reconstrução por edifício (Fases 2–3: uma parte por edifício).

Para cada quadrícula com LAZ e MDT:

1. lê os pontos de edifício (classe 6) da zona, por blocos;
2. estima o deslocamento global footprint ↔ LiDAR da quadrícula;
3. atribui pontos a footprints;
4. cota do terreno (percentil baixo do MDT num anel à volta do footprint);
5. ajuste do modelo de telhado → forma, parâmetros S3DB e altura total;
6. sinalizações de QA.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import shapely
from shapely.geometry import Polygon, box
from shapely.geometry.base import BaseGeometry

from osm3d.config import Config
from osm3d.domain.models import Building, BuildingPart, Estimate, QAFlag, RoofShape, Source
from osm3d.geospatial.point_clouds import PointSet, assign_points, read_points
from osm3d.geospatial.raster import DtmMosaic, sample_ground_elevation
from osm3d.geospatial.registration import estimate_tile_offset
from osm3d.geospatial.tiling import TileKey, tile_of
from osm3d.reconstruction.roof_fitting import (
    FitParams,
    RoofFit,
    fit_roof,
    group_beats_individuals,
)

log = logging.getLogger(__name__)

# Densidade mínima de pontos de edifício (pts/m² de footprint) abaixo da qual se
# suspeita de edifício demolido, posterior ao voo ou mal classificado.
MIN_POINT_DENSITY = 4.0
# Área / área do retângulo mínimo orientado: abaixo disto a planta não é retangular
# e o modelo de telhado de uma só parte é pouco fiável (precisa de building:part).
MIN_RECTANGULARITY = 0.85


@dataclass
class TileReport:
    tile: str
    n_buildings: int
    n_points: int
    offset: tuple[float, float]
    iou_before: float
    iou_after: float


def _flag(code: str, message: str, severity: str = "warning") -> QAFlag:
    return QAFlag(code=code, message=message, severity=severity)  # type: ignore[arg-type]


def _shift(geom, offset: tuple[float, float]):  # type: ignore[no-untyped-def]
    dx, dy = offset
    return shapely.transform(geom, lambda c: c + np.array([dx, dy]))


def _fit_params(cfg: Config) -> FitParams:
    return FitParams(
        candidates=tuple(cfg.roofs.candidate_shapes),
        max_rmse_m=cfg.roofs.max_rmse_m,
        flat_max_slope_deg=cfg.roofs.flat_max_slope_deg,
        min_points=cfg.heights.min_points_per_building,
        height_percentile=cfg.heights.roof_percentile,
    )


def _roof_flags(fit: RoofFit, cfg: Config) -> list[QAFlag]:
    flags: list[QAFlag] = []
    if fit.roof.shape.value is RoofShape.UNKNOWN:
        flags.append(_flag("roof_shape_unknown", "Nenhum modelo de telhado se ajusta bem"))
    elif fit.roof.shape.confidence < cfg.roofs.min_shape_confidence:
        flags.append(
            _flag("roof_shape_low_confidence", f"Confiança {fit.roof.shape.confidence:.2f}")
        )
    return flags


def _height_flags(height: float) -> list[QAFlag]:
    if 2.0 <= height <= 60.0:
        return []
    return [_flag("height_outlier", f"Altura {height:.1f} m implausível")]


_ROOF_FLAG_CODES = {"roof_shape_unknown", "roof_shape_low_confidence", "height_outlier"}


def reconstruct_building(
    building: Building,
    points: PointSet,
    dtm: DtmMosaic,
    offset: tuple[float, float],
    cfg: Config,
) -> tuple[Building, RoofFit | None]:
    footprint = building.footprint
    area = float(footprint.area)
    rect_area = float(footprint.minimum_rotated_rectangle.area) or area
    diagnostics = {
        "n_points": float(len(points)),
        "point_density": len(points) / area if area else 0.0,
        "rectangularity": area / rect_area,
        "area_m2": area,
    }
    flags: list[QAFlag] = []
    if diagnostics["rectangularity"] < MIN_RECTANGULARITY:
        flags.append(
            _flag("complex_footprint", "Planta não retangular: candidata a partes", "info")
        )

    ground = sample_ground_elevation(
        dtm, footprint, cfg.heights.ground_ring_buffer_m, cfg.heights.ground_percentile
    )
    update: dict[str, object] = {
        "registration_offset_m": offset,
        "ground_elevation_m": ground,
        "diagnostics": diagnostics,
    }
    if ground is None:
        flags.append(_flag("no_ground", "Sem MDT à volta do edifício", "error"))
        return building.model_copy(update=update | {"qa_flags": flags, "parts": []}), None
    if len(points) == 0 or diagnostics["point_density"] < MIN_POINT_DENSITY:
        flags.append(
            _flag(
                "low_point_density",
                f"{diagnostics['point_density']:.1f} pts/m² de edifício: demolido, "
                "posterior ao voo ou mal classificado?",
            )
        )
        if len(points) < cfg.heights.min_points_per_building:
            return building.model_copy(update=update | {"qa_flags": flags, "parts": []}), None

    fit = fit_roof(_shift(footprint, offset), points, ground, _fit_params(cfg))
    if fit.best is not None:
        diagnostics["rmse_m"] = fit.best.rmse_m
        diagnostics["inlier_ratio"] = fit.best.inlier_ratio
    flags += _roof_flags(fit, cfg) + _height_flags(fit.height.value)

    part = BuildingPart(
        id=f"{building.key}#1", geometry=footprint, height=fit.height, roof=fit.roof
    )
    return building.model_copy(update=update | {"qa_flags": flags, "parts": [part]}), fit


def attached_clusters(footprints: list[BaseGeometry], gap_m: float = 0.3) -> list[list[int]]:
    """Grupos (≥ 2) de footprints encostados entre si, a menos de ``gap_m``."""
    parent = list(range(len(footprints)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    tree = shapely.STRtree(footprints)
    src, dst = tree.query([f.buffer(gap_m) for f in footprints], predicate="intersects")
    for i, j in zip(src.tolist(), dst.tolist(), strict=True):
        if i != j:
            parent[find(i)] = find(j)
    groups: dict[int, list[int]] = defaultdict(list)
    for i in range(len(footprints)):
        groups[find(i)].append(i)
    return [g for g in groups.values() if len(g) >= 2]


def apply_shared_roofs(
    results: list[Building],
    fits: list[RoofFit | None],
    point_sets: list[PointSet],
    offset: tuple[float, float],
    cfg: Config,
    max_group: int = 12,
) -> int:
    """Telhados contínuos sobre edifícios encostados (casas geminadas/em banda).

    Para cada grupo de edifícios encostados com planta conjunta retangular, ajusta um
    único telhado ao conjunto. Se esse telhado explicar os pontos melhor do que os
    telhados individuais (mesmo critério penalizado), cada edifício recebe a forma,
    altura de telhado e orientação do conjunto. Devolve o n.º de edifícios alterados.
    """
    changed = 0
    params = _fit_params(cfg)
    for cluster in attached_clusters([b.footprint for b in results]):
        members = [
            i
            for i in cluster
            if (f := fits[i]) is not None
            and f.best is not None
            and results[i].ground_elevation_m is not None
        ]
        if not 2 <= len(members) <= max_group:
            continue
        # Fecha as pequenas folgas entre paredes antes de medir a planta conjunta.
        union = shapely.union_all(
            [results[i].footprint.buffer(0.3, join_style="mitre") for i in members]
        ).buffer(-0.3, join_style="mitre")
        if not isinstance(union, Polygon) or union.is_empty:
            continue
        if union.area / union.minimum_rotated_rectangle.area < MIN_RECTANGULARITY:
            continue

        grounds = [results[i].ground_elevation_m for i in members]
        pts = PointSet.concat(point_sets[i] for i in members)
        group_fit = fit_roof(
            _shift(union, offset), pts, min(g for g in grounds if g is not None), params
        )
        if (
            group_fit.best is None
            or group_fit.roof.shape.value is RoofShape.UNKNOWN
            or group_fit.roof.shape.confidence < cfg.roofs.min_shape_confidence
            or not group_beats_individuals(group_fit.best, [fits[i].best for i in members])  # type: ignore[union-attr,misc]
        ):
            continue

        group_keys = ",".join(results[i].key for i in members)
        for i in members:
            b = results[i]
            assert b.ground_elevation_m is not None
            height = Estimate[float](
                value=group_fit.best.top_z - b.ground_elevation_m,
                confidence=group_fit.height.confidence,
                source=Source.LIDAR,
            )
            part = BuildingPart(
                id=f"{b.key}#1", geometry=b.footprint, height=height, roof=group_fit.roof
            )
            flags = [f for f in b.qa_flags if f.code not in _ROOF_FLAG_CODES]
            flags += _height_flags(height.value)
            flags.append(_flag("shared_roof", f"Telhado contínuo com: {group_keys}", "info"))
            diagnostics = dict(b.diagnostics, roof_group_size=float(len(members)))
            results[i] = b.model_copy(
                update={"parts": [part], "qa_flags": flags, "diagnostics": diagnostics}
            )
            changed += 1
    return changed


def reconstruct_area(
    buildings: list[Building],
    lidar_tiles: dict[str, Path],
    dtm_tiles: dict[str, Path],
    cfg: Config,
    progress: Callable[[str], None] | None = None,
) -> tuple[list[Building], list[TileReport]]:
    """Reconstrói os edifícios cobertos por LAZ + MDT. Os restantes não são devolvidos."""
    available = sorted(set(lidar_tiles) & set(dtm_tiles))
    if not available:
        raise RuntimeError("Sem quadrículas com LAZ e MDT descarregados (osm3d dgt ...)")
    coverage = shapely.union_all([box(*TileKey.from_dgt_name(t).bounds) for t in available])
    covered = [b for b in buildings if b.footprint.within(coverage)]
    log.info("%d de %d edifícios dentro da cobertura LiDAR", len(covered), len(buildings))

    by_tile: dict[str, list[Building]] = defaultdict(list)
    for b in covered:
        c = b.footprint.centroid
        by_tile[tile_of(c.x, c.y).dgt_name].append(b)

    margin = cfg.footprints.registration_max_shift_m + 1.0
    results: list[Building] = []
    reports: list[TileReport] = []
    with DtmMosaic(dtm_tiles[t] for t in available) as dtm:
        for tile in sorted(by_tile):
            group = by_tile[tile]
            minx, miny, maxx, maxy = shapely.union_all([b.footprint for b in group]).bounds
            bbox = (minx - margin, miny - margin, maxx + margin, maxy + margin)
            points = read_points(
                (lidar_tiles[t] for t in available), bbox, cfg.heights.building_classes
            )
            offset, iou0, iou1 = estimate_tile_offset(
                [b.footprint for b in group],
                points,
                bbox,
                max_shift_m=cfg.footprints.registration_max_shift_m,
            )
            reports.append(TileReport(tile, len(group), len(points), offset, iou0, iou1))
            if progress:
                progress(
                    f"  quadrícula {tile}: {len(group)} edifícios, {len(points):,} pontos, "
                    f"desloc. ({offset[0]:+.2f}, {offset[1]:+.2f}) m, IoU {iou0:.3f}→{iou1:.3f}"
                )
            indices = assign_points(points, [b.footprint for b in group], offset)
            point_sets = [points.subset(idx) for idx in indices]
            tile_results: list[Building] = []
            fits: list[RoofFit | None] = []
            for b, pts in zip(group, point_sets, strict=True):
                built, fit = reconstruct_building(b, pts, dtm, offset, cfg)
                tile_results.append(built)
                fits.append(fit)
            shared = apply_shared_roofs(tile_results, fits, point_sets, offset, cfg)
            if progress and shared:
                progress(f"  quadrícula {tile}: {shared} edifícios com telhado partilhado")
            results.extend(tile_results)
    return results, reports
