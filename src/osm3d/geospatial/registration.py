"""Co-registo footprint OSM ↔ LiDAR.

Os contornos OSM são frequentemente desenhados sobre imagem com desvio de 1–3 m.
Estima-se a translação (dx, dy) que maximiza a sobreposição entre o footprint e a
máscara de pontos de edifício (classe 6), numa grelha até ``max_shift_m``.
O desvio é usado apenas para selecionar pontos — a geometria OSM não é alterada.
"""

from __future__ import annotations

from osm3d.domain.models import Building
from osm3d.geospatial.point_clouds import PointSet


def estimate_offset(
    building: Building, points: PointSet, max_shift_m: float, step_m: float
) -> tuple[float, float]:
    raise NotImplementedError("Fase 1: co-registo")
