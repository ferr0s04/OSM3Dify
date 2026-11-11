"""Cor dominante de telhado a partir do RGB do LiDAR (principal) ou de ortofotos.

O LiDAR da DGT vem colorido mas não tem pontos nas fachadas
(ver data/reference/dgt-lidar-rgb-sem-fachadas.png).

1. Usar o RGB dos pontos inliers do modelo de telhado ajustado, excluindo uma faixa
   no bordo (os bordos têm pontos com a cor do chão/vegetação). Fallback: pixels da
   ortofoto numa máscara de telhado derivada do LiDAR (erodida).
2. Converter para CIELAB (scikit-image), descartar sombras/realces.
3. Clustering (k-means) → cluster dominante.
4. Aproximar à paleta configurada (ΔE mínimo); confiança pela dominância e ΔE.
"""

from __future__ import annotations

import numpy as np
import numpy.typing as npt

from osm3d.domain.models import Estimate


def dominant_roof_colour(
    rgb_pixels: npt.NDArray[np.uint8], palette: list[str]
) -> Estimate[str] | None:
    raise NotImplementedError("Fase 5: cor de telhado")
