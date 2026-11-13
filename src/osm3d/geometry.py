"""Utilitários de orientação partilhados (reconstrução e exportação).

Convenção: CRS métrico com x = este e y = norte; azimutes em graus, 0 = norte,
sentido horário. Um *eixo* é uma direção sem sentido, em [0, 180).
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from shapely.geometry import Polygon
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


def remove_spikes(poly: Polygon, min_turn_deg: float = 178.0, min_len_m: float = 0.01) -> Polygon:
    """Tira vértices repetidos e "espigões" (o contorno vai e volta pelo mesmo caminho).

    Um espigão tem área nula, por isso o polígono continua "válido", mas a ida-e-volta
    parte operações como ``segmentize`` (devolve um multipolígono) e a malha 3D, e não
    deve ir para o OSM. Remove-se o vértice onde o contorno inverte o sentido
    (viragem ≥ ``min_turn_deg``), até não haver mais.
    """
    if not isinstance(poly, Polygon) or poly.is_empty:
        return poly

    def clean(ring: list[tuple[float, float]]) -> list[tuple[float, float]]:
        pts = ring[:-1]
        changed = True
        while changed and len(pts) >= 3:
            changed = False
            for i in range(len(pts)):
                a, b, c = pts[i - 1], pts[i], pts[(i + 1) % len(pts)]
                ab = (b[0] - a[0], b[1] - a[1])
                bc = (c[0] - b[0], c[1] - b[1])
                lab, lbc = math.hypot(*ab), math.hypot(*bc)
                if lab < min_len_m:
                    del pts[i]
                    changed = True
                    break
                if lbc < min_len_m:
                    continue
                cos = (ab[0] * bc[0] + ab[1] * bc[1]) / (lab * lbc)
                turn = math.degrees(math.acos(max(-1.0, min(1.0, cos))))
                if turn >= min_turn_deg:
                    del pts[i]
                    changed = True
                    break
        return [*pts, pts[0]] if len(pts) >= 3 else []

    shell = clean(list(poly.exterior.coords))
    if not shell:
        return poly
    holes = [h for r in poly.interiors if (h := clean(list(r.coords)))]
    out = Polygon(shell, holes)
    return out if out.is_valid and not out.is_empty else poly
