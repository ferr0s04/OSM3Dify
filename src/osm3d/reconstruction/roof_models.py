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
from shapely.geometry.base import BaseGeometry

from osm3d.domain.models import RoofShape
from osm3d.geometry import oriented_rectangle

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

    def to_world(self, u: Array, v: Array) -> tuple[Array, Array]:
        theta = np.radians(self.azimuth_deg)
        ux, uy = np.sin(theta), np.cos(theta)
        return self.cx + u * ux - v * uy, self.cy + u * uy + v * ux

    @property
    def v_azimuth_deg(self) -> float:
        """Azimute do sentido +v (90° à esquerda de +u)."""
        return (self.azimuth_deg - 90.0) % 360.0

    def rotated(self) -> LocalFrame:
        """O mesmo retângulo com o eixo u rodado 90° (troca comprimento e largura)."""
        return LocalFrame(
            self.cx, self.cy, (self.azimuth_deg + 90.0) % 180.0, self.half_width, self.half_length
        )

    @classmethod
    def from_geometry(cls, geometry: BaseGeometry) -> LocalFrame:
        """Referencial com u ao longo do lado maior do retângulo mínimo orientado."""
        r = oriented_rectangle(geometry)
        return cls(r.cx, r.cy, r.azimuth_deg, r.half_length, r.half_width)


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
