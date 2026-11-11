"""Co-registo footprint OSM ↔ LiDAR: um deslocamento global por quadrícula.

Rasteriza os footprints e os pontos de edifício (classe 6) e procura a translação
(dx, dy) que maximiza o IoU das duas máscaras. A otimização edifício a edifício foi
testada e descartada (ver docs/area-de-estudo.md): é instável e o ganho é marginal.
O deslocamento só é usado para selecionar pontos; a geometria OSM não é alterada.
"""

from __future__ import annotations

from collections.abc import Sequence

import numpy as np
from rasterio.features import rasterize
from rasterio.transform import from_origin
from scipy import ndimage
from shapely.geometry.base import BaseGeometry

from osm3d.geospatial.point_clouds import PointSet


def _iou(a: np.ndarray, b: np.ndarray) -> float:
    union = int((a | b).sum())
    return float((a & b).sum() / union) if union else 0.0


def estimate_tile_offset(
    footprints: Sequence[BaseGeometry],
    building_points: PointSet,
    bounds: tuple[float, float, float, float],
    *,
    resolution_m: float = 0.5,
    max_shift_m: float = 3.0,
) -> tuple[tuple[float, float], float, float]:
    """Devolve ((dx, dy), IoU sem deslocamento, IoU com deslocamento)."""
    minx, miny, maxx, maxy = bounds
    ncols = int(np.ceil((maxx - minx) / resolution_m))
    nrows = int(np.ceil((maxy - miny) / resolution_m))
    if not footprints or len(building_points) == 0:
        return (0.0, 0.0), 0.0, 0.0

    lidar = np.zeros((nrows, ncols), dtype=bool)
    col = ((building_points.xyz[:, 0] - minx) / resolution_m).astype(int)
    row = ((maxy - building_points.xyz[:, 1]) / resolution_m).astype(int)
    ok = (col >= 0) & (col < ncols) & (row >= 0) & (row < nrows)
    lidar[row[ok], col[ok]] = True
    lidar = ndimage.binary_closing(lidar, iterations=2)

    transform = from_origin(minx, maxy, resolution_m, resolution_m)
    fp = rasterize([(f, 1) for f in footprints], out_shape=lidar.shape, transform=transform)
    fp = fp.astype(bool)

    base = _iou(fp, lidar)
    best = (base, 0, 0)
    steps = int(max_shift_m / resolution_m)
    for di in range(-steps, steps + 1):
        rolled = np.roll(fp, di, axis=0)
        for dj in range(-steps, steps + 1):
            score = _iou(np.roll(rolled, dj, axis=1), lidar)
            if score > best[0]:
                best = (score, di, dj)
    score, di, dj = best
    return (dj * resolution_m, -di * resolution_m), base, score
