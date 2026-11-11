"""Modelos paramétricos de telhado (abordagem model-driven).

Cada modelo descreve a cota do telhado num referencial local do retângulo mínimo
orientado da parte:

- ``u``: coordenada ao longo do eixo do modelo (cumeeira em gabled/hipped), em [-a, a]
- ``v``: coordenada perpendicular, em [-b, b]
- ``z0``: cota do beiral; ``h``: altura do telhado (``roof:height``)

O ajuste (``roof_fitting``) testa cada modelo com o eixo ao longo e através do lado
maior, escolhe por critério penalizado e devolve diretamente os parâmetros S3DB.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import numpy.typing as npt

from osm3d.domain.models import RoofShape

Array = npt.NDArray[np.float64]

# Número de parâmetros livres por modelo (z0, h, ...) — para BIC/AIC.
N_PARAMS: dict[RoofShape, int] = {
    RoofShape.FLAT: 1,
    RoofShape.SKILLION: 2,
    RoofShape.GABLED: 2,
    RoofShape.HIPPED: 2,
    RoofShape.PYRAMIDAL: 2,
}


@dataclass(frozen=True)
class LocalFrame:
    """Referencial local: origem no centro, eixo ``u`` com azimute ``azimuth_deg``."""

    cx: float
    cy: float
    azimuth_deg: float
    half_length: float  # a
    half_width: float  # b

    def to_local(self, x: Array, y: Array) -> tuple[Array, Array]:
        theta = np.radians(self.azimuth_deg)
        # Vetor unitário do eixo u (azimute: 0 = norte, horário): (sin θ, cos θ).
        ux, uy = np.sin(theta), np.cos(theta)
        dx, dy = x - self.cx, y - self.cy
        u = dx * ux + dy * uy
        v = -dx * uy + dy * ux
        return u, v


def roof_surface(
    shape: RoofShape, u: Array, v: Array, a: float, b: float, z0: float, h: float
) -> Array:
    """Cota do telhado para cada ponto (u, v) do referencial local."""
    if a <= 0 or b <= 0:
        raise ValueError("Dimensões do referencial têm de ser positivas")
    au, av = np.abs(u), np.abs(v)
    match shape:
        case RoofShape.FLAT:
            rel = np.zeros_like(u)
        case RoofShape.SKILLION:
            # Sobe linearmente de v = +b (beiral) até v = -b (topo).
            rel = (b - v) / (2 * b)
        case RoofShape.GABLED:
            rel = 1 - av / b
        case RoofShape.HIPPED:
            # Águas com a mesma inclinação; cumeeira de comprimento 2(a - b) se a > b.
            rel = np.minimum(1 - av / b, (a - au) / b)
        case RoofShape.PYRAMIDAL:
            rel = np.minimum(1 - au / a, 1 - av / b)
        case _:
            raise ValueError(f"Forma sem modelo paramétrico: {shape}")
    return z0 + h * np.clip(rel, 0.0, 1.0)
