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
from shapely.geometry import box

from osm3d.config import Config
from osm3d.domain.models import Building, BuildingPart, QAFlag, RoofShape
from osm3d.geospatial.point_clouds import PointSet, assign_points, read_points
from osm3d.geospatial.raster import DtmMosaic, sample_ground_elevation
from osm3d.geospatial.registration import estimate_tile_offset
from osm3d.geospatial.tiling import TileKey, tile_of
from osm3d.reconstruction.roof_fitting import FitParams, fit_roof

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


def reconstruct_building(
    building: Building,
    points: PointSet,
    dtm: DtmMosaic,
    offset: tuple[float, float],
    cfg: Config,
) -> Building:
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
        return building.model_copy(update=update | {"qa_flags": flags, "parts": []})
    if len(points) == 0 or diagnostics["point_density"] < MIN_POINT_DENSITY:
        flags.append(
            _flag(
                "low_point_density",
                f"{diagnostics['point_density']:.1f} pts/m² de edifício: demolido, "
                "posterior ao voo ou mal classificado?",
            )
        )
        if len(points) < cfg.heights.min_points_per_building:
            return building.model_copy(update=update | {"qa_flags": flags, "parts": []})

    params = FitParams(
        candidates=tuple(cfg.roofs.candidate_shapes),
        max_rmse_m=cfg.roofs.max_rmse_m,
        flat_max_slope_deg=cfg.roofs.flat_max_slope_deg,
        min_points=cfg.heights.min_points_per_building,
        height_percentile=cfg.heights.roof_percentile,
    )
    fit = fit_roof(_shift(footprint, offset), points, ground, params)
    if fit.best is not None:
        diagnostics["rmse_m"] = fit.best.rmse_m
        diagnostics["inlier_ratio"] = fit.best.inlier_ratio
    if fit.roof.shape.value is RoofShape.UNKNOWN:
        flags.append(_flag("roof_shape_unknown", "Nenhum modelo de telhado se ajusta bem"))
    elif fit.roof.shape.confidence < cfg.roofs.min_shape_confidence:
        flags.append(
            _flag("roof_shape_low_confidence", f"Confiança {fit.roof.shape.confidence:.2f}")
        )
    if not 2.0 <= fit.height.value <= 60.0:
        flags.append(_flag("height_outlier", f"Altura {fit.height.value:.1f} m implausível"))

    part = BuildingPart(
        id=f"{building.key}#1", geometry=footprint, height=fit.height, roof=fit.roof
    )
    return building.model_copy(update=update | {"qa_flags": flags, "parts": [part]})


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
            for b, idx in zip(group, indices, strict=True):
                results.append(reconstruct_building(b, points.subset(idx), dtm, offset, cfg))
    return results, reports
