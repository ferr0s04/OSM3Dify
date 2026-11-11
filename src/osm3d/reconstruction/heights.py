"""Estimativa de altura (Fase 2).

``height ≈ P{roof_percentile}(Z pontos de telhado) − cota do terreno``, com a cota
do terreno tirada de um anel à volta do footprint no MDT do mesmo levantamento.
Na Fase 3 a altura passa a vir do topo do modelo de telhado ajustado.
"""

from __future__ import annotations

import numpy as np
import numpy.typing as npt

from osm3d.domain.models import Estimate, Source


def height_from_points(
    roof_z: npt.NDArray[np.float64], ground_z: float, percentile: float = 95.0
) -> Estimate[float]:
    """Altura total (chão → topo do telhado) a partir de cotas de pontos de telhado."""
    if roof_z.size == 0:
        raise ValueError("Sem pontos de telhado")
    top = float(np.percentile(roof_z, percentile))
    height = top - ground_z
    if height <= 0:
        raise ValueError(f"Altura não positiva ({height:.2f} m): verificar cota do terreno")
    # Confiança provisória baseada no número de pontos; a calibrar com o conjunto de referência.
    confidence = float(min(1.0, roof_z.size / 200.0))
    return Estimate[float](value=height, confidence=confidence, source=Source.LIDAR)
