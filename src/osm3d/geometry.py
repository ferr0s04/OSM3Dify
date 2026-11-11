"""Utilitários de orientação partilhados (reconstrução e exportação).

Convenção: CRS métrico com x = este e y = norte; azimutes em graus, 0 = norte,
sentido horário. Um *eixo* é uma direção sem sentido, em [0, 180).
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from shapely.geometry.base import BaseGeometry


@dataclass(frozen=True)
class OrientedRectangle:
    """Retângulo mínimo orientado: centro, azimute do lado maior e semi-dimensões."""

    cx: float
    cy: float
    azimuth_deg: float  # eixo do lado maior, [0, 180)
    half_length: float  # metade do lado maior
    half_width: float  # metade do lado menor


def oriented_rectangle(geometry: BaseGeometry) -> OrientedRectangle:
    rect = geometry.minimum_rotated_rectangle
    coords = list(rect.exterior.coords)[:4]
    edges = []
    for (x0, y0), (x1, y1) in zip(coords, coords[1:] + coords[:1], strict=True):
        dx, dy = x1 - x0, y1 - y0
        edges.append((math.hypot(dx, dy), math.degrees(math.atan2(dx, dy)) % 180.0))
    edges.sort(reverse=True)
    (long_len, long_az), (short_len, _) = edges[0], edges[-1]
    c = rect.centroid
    return OrientedRectangle(c.x, c.y, long_az, long_len / 2, short_len / 2)


def longest_side_azimuth_deg(geometry: BaseGeometry) -> float:
    """Azimute (eixo) do lado maior do retângulo mínimo orientado.

    É a referência que os renderizadores S3DB usam para ``roof:orientation``.
    """
    return oriented_rectangle(geometry).azimuth_deg


def axis_difference_deg(a: float, b: float) -> float:
    """Diferença entre dois eixos (não direções), em [0, 90]."""
    d = abs(a - b) % 180.0
    return min(d, 180.0 - d)


def direction_difference_deg(a: float, b: float) -> float:
    """Diferença entre duas direções (com sentido), em [0, 180]."""
    d = abs(a - b) % 360.0
    return min(d, 360.0 - d)
