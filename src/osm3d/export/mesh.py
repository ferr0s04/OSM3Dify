"""Malha 3D de uma parte, gerada a partir do mesmo modelo paramétrico do ajuste.

O telhado é amostrado numa grelha dentro do footprint (mais o contorno densificado)
e triangulado. As paredes seguem a cota do telhado ao longo do contorno, o que
desenha as empenas. Serve a revisão (vista 3D) e, mais tarde, a exportação GLB.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import numpy.typing as npt
import shapely
from scipy.spatial import Delaunay
from shapely.geometry import MultiPolygon, Polygon
from shapely.geometry.base import BaseGeometry

from osm3d.domain.models import RoofModel, RoofShape
from osm3d.reconstruction.roof_models import model_surface_z

Array = npt.NDArray[np.float64]


@dataclass(frozen=True)
class Mesh:
    vertices: Array  # (N, 3)
    faces: npt.NDArray[np.int64]  # (M, 3)

    def to_json(self, origin: tuple[float, float, float], decimals: int = 2) -> dict[str, list]:  # type: ignore[type-arg]
        v = self.vertices - np.asarray(origin)
        return {
            "vertices": np.round(v, decimals).ravel().tolist(),
            "faces": self.faces.ravel().tolist(),
        }


roof_z = model_surface_z  # nome usado pela malha e pelos testes


def _polygons(geom: BaseGeometry) -> list[Polygon]:
    if isinstance(geom, Polygon):
        return [geom]
    if isinstance(geom, MultiPolygon):
        return list(geom.geoms)
    return []


def roof_mesh(
    footprint: BaseGeometry,
    model: RoofModel | None,
    shape: RoofShape,
    flat_z: float,
    resolution_m: float = 0.5,
) -> Mesh:
    verts: list[Array] = []
    faces: list[npt.NDArray[np.int64]] = []
    offset = 0
    for poly in _polygons(footprint):
        dense = shapely.segmentize(poly, resolution_m)
        boundary = np.vstack(
            [np.asarray(r.coords)[:-1] for r in (dense.exterior, *dense.interiors)]
        )
        minx, miny, maxx, maxy = poly.bounds
        gx, gy = np.meshgrid(
            np.arange(minx + resolution_m / 2, maxx, resolution_m),
            np.arange(miny + resolution_m / 2, maxy, resolution_m),
        )
        grid = np.column_stack([gx.ravel(), gy.ravel()])
        inner = grid[shapely.contains_xy(poly.buffer(-resolution_m / 4), grid[:, 0], grid[:, 1])]
        xy = np.vstack([boundary, inner])
        if len(xy) < 3:
            continue
        tri = Delaunay(xy)
        centres = xy[tri.simplices].mean(axis=1)
        keep = shapely.contains_xy(poly, centres[:, 0], centres[:, 1])
        z = roof_z(model, shape, xy[:, 0], xy[:, 1], flat_z)
        verts.append(np.column_stack([xy, z]))
        faces.append(tri.simplices[keep] + offset)
        offset += len(xy)
    if not verts:
        return Mesh(np.empty((0, 3)), np.empty((0, 3), dtype=np.int64))
    return Mesh(np.vstack(verts), np.vstack(faces).astype(np.int64))


def wall_mesh(
    footprint: BaseGeometry,
    model: RoofModel | None,
    shape: RoofShape,
    ground_z: float,
    flat_z: float,
    resolution_m: float = 0.5,
) -> Mesh:
    verts: list[Array] = []
    faces: list[list[int]] = []
    for poly in _polygons(footprint):
        dense = shapely.segmentize(poly, resolution_m)
        for ring in (dense.exterior, *dense.interiors):
            xy = np.asarray(ring.coords)
            top = roof_z(model, shape, xy[:, 0], xy[:, 1], flat_z)
            base = sum(len(v) for v in verts)
            bottom = np.column_stack([xy, np.full(len(xy), ground_z)])
            upper = np.column_stack([xy, top])
            verts.append(np.vstack([bottom, upper]))
            n = len(xy)
            for i in range(n - 1):
                a, b, c, d = base + i, base + i + 1, base + n + i + 1, base + n + i
                faces += [[a, b, c], [a, c, d]]
    if not verts:
        return Mesh(np.empty((0, 3)), np.empty((0, 3), dtype=np.int64))
    return Mesh(np.vstack(verts), np.asarray(faces, dtype=np.int64))
