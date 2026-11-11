"""Grelha de quadrículas de 1 km (EPSG:3763), alinhada com as quadrículas LiDAR da DGT.

Assume-se que as quadrículas DGT estão alinhadas a múltiplos de 1000 m em
PT-TM06; confirmar com os metadados (bbox) dos ficheiros descarregados.
"""

from __future__ import annotations

import math
from collections import Counter
from dataclasses import dataclass, field

from pyproj import Transformer

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

    def bounds_wgs84(self, crs: str = "EPSG:3763") -> tuple[float, float, float, float]:
        t = Transformer.from_crs(crs, "EPSG:4326", always_xy=True)
        x0, y0, x1, y1 = self.bounds
        lons, lats = t.transform([x0, x0, x1, x1], [y0, y1, y0, y1])
        return (min(lons), min(lats), max(lons), max(lats))

    def __str__(self) -> str:
        x0, y0, _, _ = self.bounds
        return f"{int(x0)}_{int(y0)}"


def tile_of(x: float, y: float) -> TileKey:
    return TileKey(math.floor(x / TILE_SIZE_M), math.floor(y / TILE_SIZE_M))


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
