"""Cor do telhado (``roof:colour``) a partir do RGB dos pontos LiDAR.

O LiDAR da DGT vem colorido, mas com cores desbotadas: a telha cerâmica aparece
como ~(245, 205, 185) e não como terracota. Comparar com a cor mais próxima de uma
paleta classificaria telha como branco ou cinzento. Por isso classifica-se ponto a
ponto em CIELAB, por **croma**, **tom** e **luminosidade** (robustos ao desbotamento),
e devolve-se uma cor **realista** da paleta (não a cor medida).

Calibração em Vila do Conde (502 edifícios, ver docs/area-de-estudo.md):

- C* < 7 → acromáticos (betão, chapa, branco); C* > 8 com tom 20–60° → telha;
- 98 % dos telhados hipped estão no grupo da telha.

Classifica-se a cor **mediana** do telhado. A confiança é a homogeneidade, isto é,
a fração de pontos a ΔE < 10 da mediana, multiplicada pela suficiência de pontos.

Pontos usados: classe 6, dentro do footprint erodido 0,75 m (os bordos trazem cor
do chão/vegetação) e a menos de 0,3 m da superfície do telhado ajustado (exclui
chaminés, claraboias, painéis e vegetação sobre o telhado).

A cor das fachadas não é estimada: o LiDAR aéreo não tem pontos nas fachadas.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import numpy.typing as npt
import shapely
from skimage.color import rgb2lab

from osm3d.domain.models import Building, Estimate, Source
from osm3d.geospatial.point_clouds import PointSet
from osm3d.reconstruction.roof_models import model_surface_z

BUILDING_CLASS = 6
EDGE_EROSION_M = 0.75
MAX_ROOF_DISTANCE_M = 0.3
MIN_POINTS = 20
FULL_CONFIDENCE_POINTS = 100
HOMOGENEITY_DELTA_E = 10.0  # ΔE*76 máximo à cor mediana para um ponto "concordar"

# Limiares (CIELAB) calibrados nos dados DGT de Vila do Conde.
MIN_TILE_CHROMA = 7.0
TILE_HUE_RANGE = (10.0, 75.0)
DARK_TILE_MAX_L = 70.0


@dataclass(frozen=True)
class PaletteColour:
    key: str
    label: str
    hex: str


PALETTE: dict[str, PaletteColour] = {
    c.key: c
    for c in (
        PaletteColour("tile", "telha", "#b5533c"),
        PaletteColour("tile_dark", "telha escura", "#8f4532"),
        PaletteColour("white", "branco", "#ebebe6"),
        PaletteColour("light_grey", "cinzento claro", "#bdbdb8"),
        PaletteColour("grey", "cinzento", "#8a8a87"),
        PaletteColour("dark_grey", "cinzento escuro", "#565656"),
    )
}

# Cor usada pelos renderizadores quando não há building:colour (só para visualizar).
DEFAULT_FACADE_HEX = "#e9e4da"
DEFAULT_ROOF_HEX = "#c8c8c8"


def classify_lab(lab: npt.NDArray[np.float64]) -> npt.NDArray[np.str_]:
    """Chave da paleta para cada cor CIELAB (N, 3)."""
    L, a, b = lab[:, 0], lab[:, 1], lab[:, 2]
    chroma = np.hypot(a, b)
    hue = np.degrees(np.arctan2(b, a)) % 360
    tile = (chroma >= MIN_TILE_CHROMA) & (hue >= TILE_HUE_RANGE[0]) & (hue <= TILE_HUE_RANGE[1])
    out = np.select(
        [
            tile & (L < DARK_TILE_MAX_L),
            tile,
            L >= 85,
            L >= 70,
            L >= 55,
        ],
        ["tile_dark", "tile", "white", "light_grey", "grey"],
        default="dark_grey",
    )
    return out


def dominant_roof_colour(rgb: npt.NDArray[np.integer]) -> Estimate[str] | None:
    """Cor dominante (hex da paleta) de pontos RGB de 8 bits (N, 3)."""
    if len(rgb) < MIN_POINTS:
        return None
    lab = rgb2lab((np.asarray(rgb, dtype=np.float64) / 255.0)[None, :, :])[0]
    # Classificar a mediana é robusto ao ruído de cada ponto (que, perto dos limiares,
    # atiraria pontos de telha escura para o lado cinzento).
    median = np.median(lab, axis=0)
    key = str(classify_lab(median[None, :])[0])
    # Confiança = homogeneidade (pontos perto da mediana) × suficiência de pontos.
    homogeneity = float((np.linalg.norm(lab - median, axis=1) < HOMOGENEITY_DELTA_E).mean())
    confidence = homogeneity * min(1.0, len(rgb) / FULL_CONFIDENCE_POINTS)
    return Estimate[str](value=PALETTE[key].hex, confidence=confidence, source=Source.LIDAR)


def roof_points_mask(b: Building, points: PointSet) -> npt.NDArray[np.bool_]:
    """Pontos que pertencem à superfície do telhado ajustado, longe dos bordos."""
    dx, dy = b.registration_offset_m
    fp = shapely.transform(b.footprint, lambda c: c + np.array([dx, dy]))
    inner = fp.buffer(-EDGE_EROSION_M)
    if inner.is_empty:
        inner = fp
    x, y, z = points.xyz[:, 0], points.xyz[:, 1], points.xyz[:, 2]
    mask = (points.classification == BUILDING_CLASS) & shapely.contains_xy(inner, x, y)
    part = b.parts[0] if b.parts else None
    if part is not None and part.roof.model is not None:
        surface = model_surface_z(part.roof.model, part.roof.shape.value, x, y, 0.0, fp)
        mask &= np.abs(z - surface) < MAX_ROOF_DISTANCE_M
    return np.asarray(mask, dtype=bool)


def estimate_roof_colour(b: Building, points: PointSet) -> Estimate[str] | None:
    if points.rgb is None or not len(points):
        return None
    rgb = points.rgb[roof_points_mask(b, points)].astype(np.int64)
    if rgb.size and rgb.max() > 255:  # LAZ em 16 bits
        rgb = rgb >> 8
    return dominant_roof_colour(rgb)
