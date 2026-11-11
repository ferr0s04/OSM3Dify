"""Ajuste de modelos de telhado a pontos LiDAR (Fase 3).

Para cada forma candidata e para cada eixo (ao longo / através do lado maior):
ajuste robusto de ``z0`` e ``h`` (mínimos quadrados com perda de Huber), cálculo
do RMSE e do BIC. A confiança vem da margem entre o melhor e o segundo modelo;
RMSE acima de ``max_rmse_m`` em todos → ``unknown`` + sinalização para revisão.
"""

from __future__ import annotations

from dataclasses import dataclass

from shapely.geometry.base import BaseGeometry

from osm3d.domain.models import Roof, RoofShape
from osm3d.geospatial.point_clouds import PointSet


@dataclass(frozen=True)
class FitResult:
    shape: RoofShape
    axis_azimuth_deg: float
    z0: float
    h: float
    rmse_m: float
    bic: float


def fit_roof(
    geometry: BaseGeometry,
    points: PointSet,
    ground_z: float,
    candidates: list[RoofShape],
    max_rmse_m: float,
) -> tuple[Roof, float]:
    """Devolve o telhado ajustado e a altura total (chão → topo) da parte."""
    raise NotImplementedError("Fase 3: ajuste de modelos de telhado")
