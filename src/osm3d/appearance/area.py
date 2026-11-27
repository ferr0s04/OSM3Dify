"""Fase ``colours``: cor do telhado de todas as partes dos edifícios cobertos por LiDAR.

Com ortofotos, a cor vem do ortofoto (confirmada pelo LiDAR); sem elas, do LiDAR.
Também usada para os candidatos (edifícios em falta no OSM).
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable
from contextlib import ExitStack
from pathlib import Path

import numpy as np
import shapely

from osm3d.appearance.roof_colours import BUILDING_CLASS, estimate_roof_colour
from osm3d.domain.models import Building, BuildingPart
from osm3d.geospatial.point_clouds import PointSet, assign_points, read_points
from osm3d.geospatial.tiling import tile_of

MARGIN_M = 4.0


def colour_parts(b: Building, points: PointSet, ortho: object | None = None) -> Building:
    """``b`` com a cor de telhado de cada parte (pontos no referencial do LiDAR)."""
    parts: list[BuildingPart] = []
    changed = False
    single = len(b.parts) == 1
    shift = np.array(b.registration_offset_m)
    for part in b.parts:
        # Cada parte com os seus pontos e o seu modelo (como em reconstruction.parts).
        if single:
            probe, pts = b, points
        else:
            probe = b.model_copy(update={"footprint": part.geometry, "parts": [part]})
            in_lidar = shapely.transform(part.geometry, lambda c: c + shift)
            pts = points.subset(shapely.contains_xy(in_lidar, points.xyz[:, 0], points.xyz[:, 1]))
        colour = estimate_roof_colour(probe, pts, ortho)  # type: ignore[arg-type]
        if colour is None:
            parts.append(part)
            continue
        roof = part.roof.model_copy(update={"colour": colour})
        parts.append(part.model_copy(update={"roof": roof}))
        changed = True
    return b.model_copy(update={"parts": parts}) if changed else b


def colour_area(
    buildings: list[Building],
    lidar_tiles: dict[str, Path],
    progress: Callable[[str], None] | None = None,
    ortho_paths: list[Path] | None = None,
    cache_dir: Path | None = None,
) -> list[Building]:
    """Cor dos telhados (``cache_dir``: cache dos pontos dos LAZ, ver ``read_points``)."""
    by_tile: dict[str, list[int]] = defaultdict(list)
    for i, b in enumerate(buildings):
        if b.parts:
            c = b.footprint.centroid
            by_tile[tile_of(c.x, c.y).dgt_name].append(i)

    out = list(buildings)
    with ExitStack() as stack:
        ortho = None
        if ortho_paths:
            from osm3d.geospatial.orthophoto import OrthoMosaic

            ortho = stack.enter_context(OrthoMosaic(ortho_paths))
        for tile, idxs in sorted(by_tile.items()):
            group = [buildings[i] for i in idxs]
            minx, miny, maxx, maxy = shapely.union_all([b.footprint for b in group]).bounds
            bbox = (minx - MARGIN_M, miny - MARGIN_M, maxx + MARGIN_M, maxy + MARGIN_M)
            points = read_points(lidar_tiles.values(), bbox, [BUILDING_CLASS], cache_dir=cache_dir)
            n_coloured = n_ortho = 0
            # Edifícios com o mesmo deslocamento de co-registo partilham a atribuição
            # dos pontos (os OSM de uma quadrícula; os candidatos têm cada um o seu).
            by_offset: dict[tuple[float, float], list[int]] = defaultdict(list)
            for i in idxs:
                by_offset[buildings[i].registration_offset_m].append(i)
            for offset, members in by_offset.items():
                assigned = assign_points(points, [buildings[i].footprint for i in members], offset)
                for i, idx in zip(members, assigned, strict=True):
                    b = colour_parts(buildings[i], points.subset(idx), ortho)
                    out[i] = b
                    colours = [p.roof.colour for p in b.parts if p.roof.colour is not None]
                    if colours:
                        n_coloured += 1
                        n_ortho += any(c.source.value == "orthophoto" for c in colours)
            if progress:
                via = f" ({n_ortho} com ortofoto)" if ortho is not None else ""
                progress(
                    f"  quadrícula {tile}: cor de telhado em {n_coloured} de {len(group)} "
                    f"edifícios{via}"
                )
    return out
