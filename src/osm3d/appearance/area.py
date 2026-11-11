"""Fase ``colours``: cor do telhado para todos os edifícios cobertos por LiDAR."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable
from pathlib import Path

import shapely

from osm3d.appearance.roof_colours import BUILDING_CLASS, estimate_roof_colour
from osm3d.domain.models import Building
from osm3d.geospatial.point_clouds import assign_points, read_points
from osm3d.geospatial.tiling import tile_of

MARGIN_M = 4.0


def colour_area(
    buildings: list[Building],
    lidar_tiles: dict[str, Path],
    progress: Callable[[str], None] | None = None,
) -> list[Building]:
    by_tile: dict[str, list[int]] = defaultdict(list)
    for i, b in enumerate(buildings):
        if b.parts:
            c = b.footprint.centroid
            by_tile[tile_of(c.x, c.y).dgt_name].append(i)

    out = list(buildings)
    for tile, idxs in sorted(by_tile.items()):
        group = [buildings[i] for i in idxs]
        minx, miny, maxx, maxy = shapely.union_all([b.footprint for b in group]).bounds
        bbox = (minx - MARGIN_M, miny - MARGIN_M, maxx + MARGIN_M, maxy + MARGIN_M)
        points = read_points(lidar_tiles.values(), bbox, [BUILDING_CLASS])
        n_coloured = 0
        # O deslocamento de co-registo é o mesmo para todos os edifícios da quadrícula.
        offset = group[0].registration_offset_m
        for i, idx in zip(
            idxs, assign_points(points, [b.footprint for b in group], offset), strict=True
        ):
            b = buildings[i]
            colour = estimate_roof_colour(b, points.subset(idx))
            if colour is None:
                continue
            part = b.parts[0]
            roof = part.roof.model_copy(update={"colour": colour})
            out[i] = b.model_copy(
                update={"parts": [part.model_copy(update={"roof": roof}), *b.parts[1:]]}
            )
            n_coloured += 1
        if progress:
            progress(
                f"  quadrícula {tile}: cor de telhado em {n_coloured} de {len(group)} edifícios"
            )
    return out
