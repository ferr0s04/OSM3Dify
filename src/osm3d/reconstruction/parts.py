"""Ajuste de telhado por parte (``building:part``).

Cada parte da divisão (``PartsLayout``) é ajustada de forma independente, com os
pontos LiDAR que caem dentro dela e o mesmo terreno de referência do edifício:

- ``height``: topo do telhado da parte;
- ``min_height``: 0 (partes assentes no terreno; corrigível na revisão);
- forma, ``roof:height``, orientação e direção do telhado;
- cor do telhado a partir do RGB dos pontos da parte.

O pipeline usa isto para a sugestão automática (volume principal + volumes
secundários) e a aplicação de revisão usa-o a cada edição da divisão.
"""

from __future__ import annotations

import numpy as np
import shapely

from osm3d.appearance.roof_colours import estimate_roof_colour
from osm3d.domain.models import (
    Building,
    BuildingPart,
    Estimate,
    PartsLayout,
    Roof,
    RoofShape,
    Source,
)
from osm3d.geospatial.point_clouds import PointSet
from osm3d.reconstruction.roof_fitting import FitParams, fit_roof

MIN_PART_POINTS = 20


def _shift(geom, offset: tuple[float, float]):  # type: ignore[no-untyped-def]
    dx, dy = offset
    return shapely.transform(geom, lambda c: c + np.array([dx, dy]))


def _empty_part(pid: str, poly, height: float) -> BuildingPart:  # type: ignore[no-untyped-def]
    return BuildingPart(
        id=pid,
        geometry=poly,
        height=Estimate[float](value=max(height, 0.5), confidence=0.0, source=Source.LIDAR),
        roof=Roof(
            shape=Estimate[RoofShape](value=RoofShape.UNKNOWN, confidence=0.0, source=Source.LIDAR)
        ),
    )


def fit_layout(
    building: Building, layout: PartsLayout, points: PointSet, params: FitParams
) -> PartsLayout:
    """Ajusta cada parte e devolve a divisão com ``fitted`` preenchido.

    ``points`` estão no referencial do LiDAR (com o deslocamento de co-registo). As
    partes estão no referencial do OSM e são deslocadas para selecionar os pontos.
    """
    ground = building.ground_elevation_m
    if ground is None:
        raise ValueError("Edifício sem cota do terreno")
    fallback_height = building.parts[0].height.value if building.parts else 3.0
    offset = building.registration_offset_m
    building_pts = points.subset(points.classification == 6) if len(points) else points
    xy_x = building_pts.xyz[:, 0] if len(building_pts) else np.empty(0)
    xy_y = building_pts.xyz[:, 1] if len(building_pts) else np.empty(0)
    fitted: list[BuildingPart] = []
    for i in range(len(layout.parts)):
        poly = layout.polygon(i)
        pid = f"{building.key}#{i + 1}"
        shifted = _shift(poly, offset)
        inside = (
            shapely.contains_xy(shifted, xy_x, xy_y) if len(building_pts) else np.zeros(0, bool)
        )
        pts = building_pts.subset(inside) if len(building_pts) else building_pts
        if len(pts) < MIN_PART_POINTS:
            fitted.append(_empty_part(pid, poly, fallback_height))
            continue
        fit = fit_roof(shifted, pts, ground, params)
        part = BuildingPart(id=pid, geometry=poly, height=fit.height, roof=fit.roof)
        # Cor do telhado com os pontos desta parte (máscara sobre o seu próprio modelo).
        probe = building.model_copy(update={"footprint": poly, "parts": [part]})
        colour = estimate_roof_colour(probe, pts)
        if colour is not None:
            part = part.model_copy(update={"roof": part.roof.model_copy(update={"colour": colour})})
        fitted.append(part)
    return layout.model_copy(update={"fitted": fitted})
