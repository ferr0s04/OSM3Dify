"""Gerador de nuvens de pontos sintéticas de telhados, para testar o ajuste."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from shapely.geometry import Polygon

from osm3d.domain.models import RoofShape
from osm3d.geospatial.point_clouds import PointSet
from osm3d.reconstruction.roof_models import LocalFrame, roof_surface

# Centro perto da quadrícula de desenvolvimento (EPSG:3763).
CX, CY = -44_500.0, 179_500.0


@dataclass(frozen=True)
class SyntheticRoof:
    footprint: Polygon
    points: PointSet
    ground_z: float
    eave_z: float
    roof_height: float

    @property
    def total_height(self) -> float:
        return self.eave_z + self.roof_height - self.ground_z


def make_roof(
    shape: RoofShape,
    *,
    length: float = 16.0,
    width: float = 9.0,
    azimuth_deg: float = 30.0,
    across: bool = False,
    flip: bool = False,
    ground_z: float = 100.0,
    eave_z: float = 106.0,
    roof_height: float = 3.0,
    density: float = 10.0,  # pts/m², como o LiDAR DGT
    noise_m: float = 0.04,
    vegetation_fraction: float = 0.0,
    chimney: bool = False,
    ridge_ratio: float | None = None,
    seed: int = 0,
) -> SyntheticRoof:
    rng = np.random.default_rng(seed)
    frame = LocalFrame(CX, CY, azimuth_deg, length / 2, width / 2)
    model_frame = frame.rotated() if across else frame

    n = int(density * length * width)
    u = rng.uniform(-frame.half_length, frame.half_length, n)
    v = rng.uniform(-frame.half_width, frame.half_width, n)
    x, y = frame.to_world(u, v)

    mu, mv = model_frame.to_local(x, y)
    a, b = model_frame.half_length, model_frame.half_width
    z = roof_surface(shape, mu, mv, a, b, eave_z, roof_height, ridge_ratio=ridge_ratio, flip=flip)
    z = z + rng.normal(0, noise_m, n)

    if chimney:
        mask = (np.abs(u - frame.half_length / 2) < 0.4) & (np.abs(v - frame.half_width / 3) < 0.4)
        z[mask] += 1.2
    if vegetation_fraction > 0:
        veg = rng.random(n) < vegetation_fraction
        z[veg] += rng.uniform(0.6, 4.0, int(veg.sum()))

    cu = np.array([-1, 1, 1, -1]) * frame.half_length
    cv = np.array([-1, -1, 1, 1]) * frame.half_width
    corners = np.column_stack(frame.to_world(cu, cv))
    points = PointSet(np.column_stack([x, y, z]), np.full(n, 6, dtype=np.uint8))
    return SyntheticRoof(Polygon(corners), points, ground_z, eave_z, roof_height)
