"""Grelha de quadrículas de 1 km (EPSG:3763), igual à grelha LiDAR 2024–2025 da DGT.

Confirmado com a grelha oficial (``LiDAR2024_2025_Seccionamento.gpkg``, 91 196
quadrículas): todas estão alinhadas a múltiplos de 1000 m em PT-TM06, e o
identificador DGT (``NAME``, p.ex. ``155480`` em ``LO-155480-MM-AAAA.laz``) é
``f"{x0/1000 + 200:03d}{y_topo/1000 + 300:03d}"`` (regra verificada em 100 % da grelha).
"""

from __future__ import annotations

import math
from collections import Counter
from dataclasses import dataclass, field

import numpy as np
import shapely
from pyproj import Transformer
from shapely.geometry.base import BaseGeometry

from osm3d.domain.models import Building

TILE_SIZE_M = 1000.0


@dataclass(frozen=True, order=True)
class TileKey:
    ix: int
    iy: int

    @property
    def bounds(self) -> tuple[float, float, float, float]:
        x0, y0 = self.ix * TILE_SIZE_M, self.iy * TILE_SIZE_M
        return (x0, y0, x0 + TILE_SIZE_M, y0 + TILE_SIZE_M)

    def bounds_wgs84(
        self, crs: str = "EPSG:3763", inset_m: float = 0.0
    ) -> tuple[float, float, float, float]:
        """Bbox WGS84; ``inset_m`` encolhe-a para não tocar nas quadrículas vizinhas."""
        t = Transformer.from_crs(crs, "EPSG:4326", always_xy=True)
        x0, y0, x1, y1 = self.bounds
        x0, y0, x1, y1 = x0 + inset_m, y0 + inset_m, x1 - inset_m, y1 - inset_m
        lons, lats = t.transform([x0, x0, x1, x1], [y0, y1, y0, y1])
        return (min(lons), min(lats), max(lons), max(lats))

    @property
    def dgt_name(self) -> str:
        """Identificador da quadrícula na grelha LiDAR da DGT (campo ``NAME``)."""
        return f"{self.ix + 200:03d}{self.iy + 1 + 300:03d}"

    @classmethod
    def from_dgt_name(cls, name: str) -> TileKey:
        if len(name) != 6 or not name.isdigit():
            raise ValueError(f"Identificador DGT inválido: {name!r}")
        return cls(int(name[:3]) - 200, int(name[3:]) - 301)

    def __str__(self) -> str:
        x0, y0, _, _ = self.bounds
        return f"{int(x0)}_{int(y0)}"


def tile_of(x: float, y: float) -> TileKey:
    return TileKey(math.floor(x / TILE_SIZE_M), math.floor(y / TILE_SIZE_M))


def tiles_in_bbox(
    bbox_wgs84: tuple[float, float, float, float], crs: str = "EPSG:3763"
) -> list[TileKey]:
    """Quadrículas que intersetam uma bbox WGS84 (min_lon, min_lat, max_lon, max_lat)."""
    t = Transformer.from_crs("EPSG:4326", crs, always_xy=True)
    w, s, e, n = bbox_wgs84
    xs, ys = t.transform([w, w, e, e], [s, n, s, n])
    lo, hi = tile_of(min(xs), min(ys)), tile_of(max(xs), max(ys))
    return [TileKey(ix, iy) for ix in range(lo.ix, hi.ix + 1) for iy in range(lo.iy, hi.iy + 1)]


def tiles_in_geometry(geom_wgs84: BaseGeometry, crs: str = "EPSG:3763") -> list[TileKey]:
    """Quadrículas com interior em comum com um polígono WGS84 (p.ex. um limite)."""
    t = Transformer.from_crs("EPSG:4326", crs, always_xy=True)
    geom = shapely.transform(geom_wgs84, lambda c: np.column_stack(t.transform(c[:, 0], c[:, 1])))
    x0, y0, x1, y1 = geom.bounds
    lo, hi = tile_of(x0, y0), tile_of(x1, y1)
    shapely.prepare(geom)
    out = []
    for ix in range(lo.ix, hi.ix + 1):
        for iy in range(lo.iy, hi.iy + 1):
            key = TileKey(ix, iy)
            cell = shapely.box(*key.bounds)
            if geom.intersects(cell) and geom.intersection(cell).area > 1.0:
                out.append(key)
    return out


def tile_rings_wgs84(keys: list[TileKey], crs: str = "EPSG:3763") -> list[list[list[float]]]:
    """Anel exterior (fechado, 5 vértices) de cada quadrícula em WGS84, para GeoJSON."""
    if not keys:
        return []
    corners = np.array(
        [
            [(x0, y0), (x1, y0), (x1, y1), (x0, y1), (x0, y0)]
            for x0, y0, x1, y1 in (k.bounds for k in keys)
        ]
    ).reshape(-1, 2)
    t = Transformer.from_crs(crs, "EPSG:4326", always_xy=True)
    lon, lat = t.transform(corners[:, 0], corners[:, 1])
    ring = np.round(np.column_stack([lon, lat]), 6).reshape(len(keys), 5, 2)
    rings: list[list[list[float]]] = ring.tolist()
    return rings


@dataclass
class TileSummary:
    key: TileKey
    n_buildings: int = 0
    n_levels: int = 0
    roof_shapes: Counter[str] = field(default_factory=Counter)
    n_crossing: int = 0  # edifícios que atravessam o limite da quadrícula

    @property
    def n_roof_shape(self) -> int:
        return sum(self.roof_shapes.values())

    @property
    def n_distinct_shapes(self) -> int:
        return len(self.roof_shapes)


def summarize_tiles(buildings: list[Building]) -> list[TileSummary]:
    """Resumo por quadrícula (cada edifício conta na quadrícula do seu centróide)."""
    tiles: dict[TileKey, TileSummary] = {}
    for b in buildings:
        c = b.footprint.centroid
        key = tile_of(c.x, c.y)
        s = tiles.setdefault(key, TileSummary(key))
        s.n_buildings += 1
        if "building:levels" in b.existing_tags:
            s.n_levels += 1
        if shape := b.existing_tags.get("roof:shape"):
            s.roof_shapes[shape] += 1
        minx, miny, maxx, maxy = b.footprint.bounds
        if tile_of(minx, miny) != tile_of(maxx, maxy):
            s.n_crossing += 1
    return sorted(tiles.values(), key=lambda s: s.key)
