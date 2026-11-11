"""Telhados por *straight skeleton* para plantas não retangulares (L, T, dobradas).

É assim que os renderizadores 3D (F4 Map, OSM2World, Streets GL) desenham
``roof:shape=hipped|gabled`` em contornos complexos. Todas as águas têm a mesma
inclinação, e a cumeeira segue o eixo do edifício, mesmo nas dobras.

- **hipped:** a altura de cada ponto é proporcional ao instante em que a frente de
  onda do contorno, a recuar à mesma velocidade em todos os lados, o atravessa.
  O recuo é o ``buffer`` negativo com cantos em esquadria (``join_style="mitre"``),
  que reproduz o esqueleto reto, incluindo os vales nos cantos reentrantes.
- **gabled:** igual, mas as **pontas** (lados curtos com os dois cantos convexos) são
  empenas verticais. Prolonga-se o contorno para lá de cada ponta antes de calcular o
  esqueleto, e a cumeeira chega assim à ponta à altura máxima, sem formar hip.

A altura é normalizada para [0, 1] (1 = cumeeira mais alta), e o telhado é
``z = beiral + roof:height × base``, como nos renderizadores.
"""

from __future__ import annotations

import math
from functools import lru_cache

import numpy as np
import numpy.typing as npt
import shapely
from shapely.geometry import MultiPolygon, Polygon
from shapely.geometry.base import BaseGeometry
from shapely.geometry.polygon import orient

from osm3d.domain.models import RoofShape

Array = npt.NDArray[np.float64]

# Abaixo desta retangularidade (área / área do retângulo mínimo), os renderizadores já
# não desenham um telhado "retangular": usa-se o esqueleto.
SKELETON_MAX_RECTANGULARITY = 0.9
SKELETON_SHAPES = (RoofShape.GABLED, RoofShape.HIPPED)

_STEP_M = 0.25
_MITRE_LIMIT = 50.0
_SIMPLIFY_M = 0.2
_GABLE_LENGTH_FACTOR = 1.25  # ponta: comprimento ≤ 1,25 × largura local
_GRID_M = 0.5


def rectangularity(geometry: BaseGeometry) -> float:
    rect = geometry.minimum_rotated_rectangle.area
    return float(geometry.area / rect) if rect else 1.0


def renders_as_skeleton(geometry: BaseGeometry, shape: RoofShape) -> bool:
    """Os renderizadores desenham este telhado por esqueleto (e não como retângulo)?"""
    return shape in SKELETON_SHAPES and rectangularity(geometry) < SKELETON_MAX_RECTANGULARITY


def _main_polygon(geometry: BaseGeometry) -> Polygon:
    if isinstance(geometry, MultiPolygon):
        geometry = max(geometry.geoms, key=lambda g: g.area)
    if not isinstance(geometry, Polygon):
        raise ValueError(f"Esperado Polygon, obtido {geometry.geom_type}")
    simplified = geometry.simplify(_SIMPLIFY_M, preserve_topology=True)
    poly = simplified if isinstance(simplified, Polygon) and not simplified.is_empty else geometry
    return orient(poly, sign=1.0)  # exterior no sentido anti-horário


def wavefront_time(poly: BaseGeometry, x: Array, y: Array, step: float = _STEP_M) -> Array:
    """Instante (m de recuo) em que a frente de onda passa por cada ponto; 0 fora."""
    t_out = np.zeros(len(x))
    remaining = shapely.contains_xy(poly, x, y)
    current: BaseGeometry = poly
    t = 0.0
    while remaining.any() and not current.is_empty:
        nxt = poly.buffer(-(t + step), join_style="mitre", mitre_limit=_MITRE_LIMIT)
        inside_next = shapely.contains_xy(nxt, x, y) if not nxt.is_empty else np.zeros(len(x), bool)
        settled = remaining & ~inside_next
        if settled.any():
            # Dentro do passo, a distância à frente atual é exata em águas planas.
            d = shapely.distance(current.boundary, shapely.points(x[settled], y[settled]))
            t_out[settled] = t + np.minimum(d, step)
        remaining &= inside_next
        current, t = nxt, t + step
    return t_out


def _convex_flags(coords: npt.NDArray[np.float64]) -> npt.NDArray[np.bool_]:
    prev = np.roll(coords, 1, axis=0)
    nxt = np.roll(coords, -1, axis=0)
    a, b = coords - prev, nxt - coords
    return np.asarray(a[:, 0] * b[:, 1] - a[:, 1] * b[:, 0] > 1e-9)  # anti-horário


def gable_edges(poly: Polygon, half_width: float) -> list[int]:
    """Índices dos lados que são pontas (empenas) de um telhado gabled por esqueleto."""
    coords = np.asarray(poly.exterior.coords)[:-1]
    convex = _convex_flags(coords)
    n = len(coords)
    out = []
    for i in range(n):
        j = (i + 1) % n
        length = float(np.hypot(*(coords[j] - coords[i])))
        if convex[i] and convex[j] and length <= 2 * half_width * _GABLE_LENGTH_FACTOR:
            out.append(i)
    return out


def _extend_gables(poly: Polygon, edges: list[int], length: float) -> BaseGeometry:
    coords = np.asarray(poly.exterior.coords)[:-1]
    n = len(coords)
    quads = []
    for i in edges:
        a, b = coords[i], coords[(i + 1) % n]
        d = b - a
        normal = np.array([d[1], -d[0]]) / math.hypot(*d)  # exterior (anti-horário)
        quads.append(Polygon([a, b, b + normal * length, a + normal * length]))
    return shapely.union_all([poly, *quads])


def _grid(poly: BaseGeometry) -> tuple[Array, Array]:
    minx, miny, maxx, maxy = poly.bounds
    gx, gy = np.meshgrid(
        np.arange(minx + _GRID_M / 2, maxx, _GRID_M), np.arange(miny + _GRID_M / 2, maxy, _GRID_M)
    )
    x, y = gx.ravel(), gy.ravel()
    keep = shapely.contains_xy(poly, x, y)
    return x[keep], y[keep]


@lru_cache(maxsize=512)
def _surface(wkb: bytes, gabled: bool) -> tuple[BaseGeometry, BaseGeometry, float] | None:
    """(polígono de cálculo, polígono original, altura máxima) para uma planta."""
    poly = _main_polygon(shapely.from_wkb(wkb))
    gx, gy = _grid(poly)
    if len(gx) == 0:
        return None
    hipped_max = float(wavefront_time(poly, gx, gy).max())
    if hipped_max <= 0:
        return None
    target: BaseGeometry = poly
    if gabled:
        edges = gable_edges(poly, hipped_max)
        if not edges:
            return None  # sem pontas identificáveis: gabled ≡ hipped, não é hipótese própria
        target = _extend_gables(poly, edges, 2 * hipped_max + 1.0)
    h_max = float(wavefront_time(target, gx, gy).max())
    return (target, poly, h_max) if h_max > 0 else None


def has_skeleton(geometry: BaseGeometry, shape: RoofShape) -> bool:
    return _surface(shapely.to_wkb(geometry), shape is RoofShape.GABLED) is not None


def skeleton_basis(geometry: BaseGeometry, shape: RoofShape, x: Array, y: Array) -> Array:
    """Altura relativa do telhado por esqueleto, em [0, 1], nos pontos (x, y)."""
    if shape not in SKELETON_SHAPES:
        raise ValueError(f"Forma sem telhado por esqueleto: {shape}")
    surface = _surface(shapely.to_wkb(geometry), shape is RoofShape.GABLED)
    if surface is None:
        surface = _surface(shapely.to_wkb(geometry), False)
    if surface is None:
        return np.zeros(len(x))
    target, _, h_max = surface
    t = wavefront_time(target, np.asarray(x, float), np.asarray(y, float))
    return np.clip(t / h_max, 0.0, 1.0)
