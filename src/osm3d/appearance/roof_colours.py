"""Cor do telhado (``roof:colour``) a partir do ortofoto e do RGB dos pontos LiDAR.

Os dois sensores são lidos **nos mesmos pontos**: os pontos LiDAR de telhado
(classe 6, dentro do footprint erodido 0,75 m e a menos de 0,3 m do telhado
ajustado, o que exclui bordos, chaminés, claraboias e vegetação). O ortofoto é
amostrado na posição de cada ponto, levada do referencial do LiDAR para o do
ortofoto (= o do footprint) pelo deslocamento de co-registo do edifício.

- **Ortofoto (principal):** cores fiéis e homogéneas (25 cm, R G B NIR). Pixels de
  vegetação (NDVI > 0,2) e de sombra (muito mais escuros do que o telhado) saem.
- **LiDAR (alternativa e confirmação):** cores desbotadas (telha ≈ (245, 205, 185)),
  ruidosas (amplitude interquartil de L* de 15–25 dentro de uma água) e mais claras.
  Usado sem ortofoto e para confirmar a cor do ortofoto.

Classifica-se em CIELAB por **croma**, **tom** e **luminosidade**, com limiares
próprios de cada sensor, e devolve-se uma cor **realista** da paleta (não a medida).
A cor do telhado é a média das medianas de cada **água**: a água ao sol e a água à
sombra pesam o mesmo, seja qual for o n.º de pontos de cada uma.

**Confiança** = concordância espacial × suficiência de pontos (× concordância dos
dois sensores). A concordância é a fração (ponderada) das células de 1,5 m cuja cor
mediana dá a mesma classe do telhado (meio ponto para classes vizinhas, p. ex. telha
e telha escura), com a luminosidade de cada célula corrigida pela da sua água. Mede
se o telhado é *mesmo* de uma só cor (painéis, remendos, materiais mistos baixam-na),
e não o ruído ponto a ponto nem a diferença de iluminação entre águas.

Limiares calibrados em Vila do Conde (área-2, 249 telhados etiquetados à vista
sobre o ORTOS-2025): ver docs/area-de-estudo.md.

A cor das fachadas não é estimada: o LiDAR aéreo não tem pontos nas fachadas.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

import numpy as np
import numpy.typing as npt
import shapely
from skimage.color import rgb2lab

from osm3d.domain.models import Building, Estimate, RoofShape, Source
from osm3d.geospatial.point_clouds import PointSet
from osm3d.reconstruction.roof_models import model_surface_z

if TYPE_CHECKING:
    from osm3d.reconstruction.ortho_outline import OrthoMosaic

BUILDING_CLASS = 6
EDGE_EROSION_M = 0.75
MAX_ROOF_DISTANCE_M = 0.3
MIN_POINTS = 20
FULL_CONFIDENCE_POINTS = 100

# Águas do telhado ajustado (declive do modelo nos pontos).
FLAT_SLOPE = 0.1  # tan(≈ 6°): abaixo disto o ponto está numa zona plana
MIN_FACET_SHARE = 0.1  # águas com menos pontos do que isto não entram na média
MIN_FACET_POINTS = 10

# Concordância espacial.
CELL_M = 1.5
MIN_CELL_POINTS = 5

# Ortofoto.
NDVI_VEGETATION = 0.2
SHADOW_DROP_L = 25.0  # pixels com L* mais de 25 abaixo da mediana do telhado: sombra
MIN_ORTHO_SAMPLES = 30
# Fusão: concordância (classes iguais/vizinhas) reforça; discordância de família baixa.
AGREE_BOOST = 0.3
DISAGREE_FACTOR = 0.6

# Formas "baixas": a telha cerâmica não cobre terraços nem coberturas de uma água
# industriais; aqui um tom quente avermelhado é chapa/tela vermelha.
LOW_PITCH_SHAPES = frozenset({RoofShape.FLAT, RoofShape.SKILLION})


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
        PaletteColour("red", "vermelho", "#a8322d"),
        PaletteColour("orange", "laranja", "#d9772b"),
        PaletteColour("green", "verde", "#4f7a5a"),
        PaletteColour("white", "branco", "#ebebe6"),
        PaletteColour("light_grey", "cinzento claro", "#bdbdb8"),
        PaletteColour("grey", "cinzento", "#8a8a87"),
        PaletteColour("dark_grey", "cinzento escuro", "#565656"),
    )
}

# Cor usada pelos renderizadores quando não há building:colour (só para visualizar).
DEFAULT_FACADE_HEX = "#e9e4da"
DEFAULT_ROOF_HEX = "#c8c8c8"

WARM = frozenset({"tile", "tile_dark", "red", "orange"})
GREYS = ("white", "light_grey", "grey", "dark_grey")
# Classes vizinhas: meia concordância (fronteiras difusas entre elas).
_NEIGHBOURS = {
    frozenset(p)
    for p in (
        ("tile", "tile_dark"),
        ("tile", "orange"),
        ("tile", "red"),
        ("tile_dark", "red"),
        ("white", "light_grey"),
        ("light_grey", "grey"),
        ("grey", "dark_grey"),
    )
}


def similarity(a: str, b: str) -> float:
    """1 para a mesma classe, 0,5 para classes vizinhas, 0 de resto."""
    if a == b:
        return 1.0
    return 0.5 if frozenset((a, b)) in _NEIGHBOURS else 0.0


def family(key: str) -> str:
    return "warm" if key in WARM else "grey" if key in GREYS else key


Classifier = Callable[[npt.NDArray[np.float64], bool], npt.NDArray[np.str_]]


def _lch(lab: npt.NDArray[np.float64]) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    L, a, b = lab[:, 0], lab[:, 1], lab[:, 2]
    chroma = np.hypot(a, b)
    hue = np.degrees(np.arctan2(b, a))  # (-180, 180]: os vermelhos ficam à volta de 0
    return L, a, chroma, hue


def _greys(L: np.ndarray, cuts: tuple[float, float, float]) -> np.ndarray:
    return np.select(
        [cuts[0] <= L, cuts[1] <= L, cuts[2] <= L], list(GREYS[:3]), default="dark_grey"
    )


def classify_lab(lab: npt.NDArray[np.float64], low_pitch: bool = False) -> npt.NDArray[np.str_]:
    """Classe da paleta de cores CIELAB (N, 3) do **LiDAR** (desbotado e claro).

    C* < 7 → acromáticos por L*; tom 10–75° → telha (escura se L* < 70); tom < 12°
    (ou < 20° em telhado baixo) com C* ≥ 10 → vermelho; 180–240° → verde.
    """
    L, _a, C, h = _lch(lab)
    hp = h % 360
    red = (C >= 10) & ((h < 12) & (h > -60) | (low_pitch & (h >= 0) & (h < 20)))
    green = (C >= 10) & (hp >= 180) & (hp < 240)
    orange = (C >= 25) & (hp >= 55) & (hp <= 80)
    tile = (C >= 7) & (hp >= 10) & (hp <= 75)
    return np.select(
        [red, green, orange, tile & (L < 70), tile],
        ["red", "green", "orange", "tile_dark", "tile"],
        default=_greys(L, (85, 70, 55)),
    )


def classify_ortho_lab(
    lab: npt.NDArray[np.float64], low_pitch: bool = False
) -> npt.NDArray[np.str_]:
    """Classe da paleta de cores CIELAB (N, 3) do **ortofoto** (ORTOS da DGT).

    Os cinzentos do ortofoto são ligeiramente azulados (tom ≈ −110°, C* ≤ 6); a telha
    tem tom 25–55° e C* 5–28; o vermelho (chapa, tela) tom −10–20°.
    """
    L, a, C, h = _lch(lab)
    warm = (C >= 4.5) & (h >= -25) & (h <= 65)
    red = warm & (C >= 8.5) & ((h < 12) | (low_pitch & (h < 22)))
    orange = warm & (C >= 15) & (h >= 58)
    green = (a <= -4.5) & (C >= 7) & (L < 75)
    tile = warm & (L + 0.8 * C >= 66)
    return np.select(
        [red, orange, green, tile, warm],
        ["red", "orange", "green", "tile", "tile_dark"],
        default=_greys(L, (80, 68, 52)),
    )


# ------------------------------------------------------------------ sensores


@dataclass(frozen=True)
class SensorColour:
    key: str
    lab: tuple[float, float, float]  # cor do telhado (média das medianas das águas)
    agreement: float  # concordância espacial, [0, 1]
    n: int

    @property
    def confidence(self) -> float:
        return self.agreement * min(1.0, self.n / FULL_CONFIDENCE_POINTS)


def to_lab(rgb: npt.ArrayLike) -> npt.NDArray[np.float64]:
    """RGB de 8 bits (N, 3) → CIELAB (N, 3)."""
    return np.asarray(rgb2lab((np.asarray(rgb, dtype=np.float64) / 255.0)[None, :, :])[0])


def sensor_colour(
    lab: npt.NDArray[np.float64],
    classify: Classifier,
    facets: npt.NDArray[np.integer] | None = None,
    xy: npt.NDArray[np.float64] | None = None,
    low_pitch: bool = False,
) -> SensorColour | None:
    """Cor de um telhado a partir das cores CIELAB (N, 3) de um sensor.

    ``facets``: água de cada ponto (0 = zona plana); ``xy``: posição de cada ponto,
    para a concordância por células. Sem eles, o telhado é uma só água e cada ponto
    é uma célula.
    """
    n = len(lab)
    if n < MIN_POINTS:
        return None
    facets = np.zeros(n, int) if facets is None else np.asarray(facets)
    ids, counts = np.unique(facets, return_counts=True)
    main = ids[counts >= max(MIN_FACET_POINTS, MIN_FACET_SHARE * n)]
    if not len(main):
        main = ids[[int(counts.argmax())]]
    medians = {int(k): np.median(lab[facets == k], axis=0) for k in ids}
    roof = np.mean([medians[int(k)] for k in main], axis=0)
    key = str(classify(roof[None, :], low_pitch)[0])

    # Luminosidade de cada ponto relativa à da sua água: a iluminação não conta.
    corrected = lab.copy()
    corrected[:, 0] += roof[0] - np.array([medians[int(k)][0] for k in facets])
    if xy is None:  # sem posições: blocos de pontos consecutivos fazem de células
        cells = np.arange(n) // MIN_CELL_POINTS
    else:
        ij = np.floor(np.asarray(xy) / CELL_M).astype(np.int64)
        _, cells = np.unique(ij, axis=0, return_inverse=True)
        cells = cells.ravel()
    order = np.argsort(cells, kind="stable")
    bounds = np.flatnonzero(np.diff(cells[order])) + 1
    score = weight = 0.0
    groups = [g for g in np.split(order, bounds) if len(g) >= MIN_CELL_POINTS]
    if not groups:  # células todas pequenas: o telhado inteiro conta como uma
        groups = [order]
    cell_lab = np.array([np.median(corrected[g], axis=0) for g in groups])
    cell_keys = classify(cell_lab, low_pitch)
    for g, k in zip(groups, cell_keys, strict=True):
        score += len(g) * similarity(str(k), key)
        weight += len(g)
    return SensorColour(key, (float(roof[0]), float(roof[1]), float(roof[2])), score / weight, n)


def fuse(lidar: SensorColour | None, ortho: SensorColour | None) -> Estimate[str] | None:
    """Cor final: a do ortofoto (se houver), confirmada ou não pela do LiDAR."""
    if ortho is None:
        if lidar is None:
            return None
        return Estimate[str](
            value=PALETTE[lidar.key].hex, confidence=lidar.confidence, source=Source.LIDAR
        )
    conf = ortho.confidence
    if lidar is not None:
        agree = similarity(lidar.key, ortho.key)
        if agree > 0:
            conf += AGREE_BOOST * agree * (1.0 - conf)
        elif family(lidar.key) != family(ortho.key):
            conf *= DISAGREE_FACTOR
    return Estimate[str](
        value=PALETTE[ortho.key].hex, confidence=float(conf), source=Source.ORTHOPHOTO
    )


def dominant_roof_colour(rgb: npt.NDArray[np.integer]) -> Estimate[str] | None:
    """Cor (hex da paleta) de pontos LiDAR RGB de 8 bits (N, 3), sem posições."""
    return fuse(sensor_colour(to_lab(rgb), classify_lab) if len(rgb) else None, None)


# ---------------------------------------------------------------- telhado


def _lidar_footprint(b: Building) -> shapely.Geometry:
    dx, dy = b.registration_offset_m
    return shapely.transform(b.footprint, lambda c: c + np.array([dx, dy]))


def roof_points_mask(b: Building, points: PointSet) -> npt.NDArray[np.bool_]:
    """Pontos que pertencem à superfície do telhado ajustado, longe dos bordos."""
    fp = _lidar_footprint(b)
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


def roof_facets(b: Building, x: np.ndarray, y: np.ndarray) -> npt.NDArray[np.int64]:
    """Água do telhado ajustado em cada ponto: 0 = plano, 1–4 = orientação da descida.

    As orientações são agrupadas de 90 em 90° a partir da orientação dominante das
    águas (média circular de 4 × azimute), o que serve a gabled, hipped e afins.
    """
    part = b.parts[0] if b.parts else None
    model = part.roof.model if part is not None else None
    if (
        part is None
        or model is None
        or part.roof.shape.value in (RoofShape.FLAT, RoofShape.UNKNOWN)
    ):
        return np.zeros(len(x), np.int64)
    fp = _lidar_footprint(b)
    shape = part.roof.shape.value

    def z(xx: np.ndarray, yy: np.ndarray) -> np.ndarray:
        return np.asarray(model_surface_z(model, shape, xx, yy, 0.0, fp))

    e = 0.25
    gx = (z(x + e, y) - z(x - e, y)) / (2 * e)
    gy = (z(x, y + e) - z(x, y - e)) / (2 * e)
    slope = np.hypot(gx, gy)
    aspect = np.arctan2(gy, gx)
    sloped = slope >= FLAT_SLOPE
    if not sloped.any():
        return np.zeros(len(x), np.int64)
    dom = np.angle(np.exp(4j * aspect[sloped]).sum()) / 4
    k = np.round((aspect - dom) / (np.pi / 2)).astype(np.int64) % 4 + 1
    return np.where(sloped, k, 0)


def sample_ortho(
    ortho: OrthoMosaic, x: np.ndarray, y: np.ndarray
) -> tuple[npt.NDArray[np.float64], npt.NDArray[np.bool_]] | None:
    """RGB (N, 3) do ortofoto em (x, y) do seu referencial e máscara dos utilizáveis.

    Fora: sem dados (preto), vegetação (NDVI > 0,2) e sombras (L* muito abaixo da
    mediana). ``None`` se nenhum ortofoto cobre os pontos.
    """
    from osm3d.reconstruction.ortho_outline import RES_M, snap_bounds

    if not len(x):
        return None
    bounds = snap_bounds((x.min() - RES_M, y.min() - RES_M, x.max() + RES_M, y.max() + RES_M))
    if not ortho.covers(bounds):
        return None
    img = ortho.read(bounds)
    if img is None:
        return None
    col = np.clip(np.floor((x - bounds[0]) / RES_M).astype(int), 0, img.shape[2] - 1)
    row = np.clip(np.floor((bounds[3] - y) / RES_M).astype(int), 0, img.shape[1] - 1)
    px = img[:, row, col].T.astype(np.float64)  # (N, 4): R G B NIR
    rgb, nir = px[:, :3], px[:, 3]
    ndvi = (nir - rgb[:, 0]) / np.maximum(nir + rgb[:, 0], 1.0)
    ok = (rgb.sum(1) > 0) & (ndvi <= NDVI_VEGETATION)
    if ok.sum() >= MIN_POINTS:
        L = to_lab(rgb[ok])[:, 0]
        dark = np.zeros(len(x), bool)
        dark[ok] = np.median(L) - SHADOW_DROP_L > L
        ok &= ~dark
    return rgb, ok


def estimate_roof_colour(
    b: Building, points: PointSet, ortho: OrthoMosaic | None = None
) -> Estimate[str] | None:
    """Cor do telhado da primeira parte de ``b`` (pontos no referencial do LiDAR)."""
    if not len(points):
        return None
    roof = points.subset(roof_points_mask(b, points))
    if len(roof) < MIN_POINTS:
        return None
    x, y = roof.xyz[:, 0], roof.xyz[:, 1]
    facets = roof_facets(b, x, y)
    part = b.parts[0] if b.parts else None
    low = part is not None and part.roof.shape.value in LOW_PITCH_SHAPES
    lidar = None
    if roof.rgb is not None:
        rgb = roof.rgb.astype(np.int64)
        if rgb.size and rgb.max() > 255:  # LAZ em 16 bits
            rgb = rgb >> 8
        lidar = sensor_colour(to_lab(rgb), classify_lab, facets, roof.xyz[:, :2], low)
    from_ortho = None
    if ortho is not None:
        dx, dy = b.registration_offset_m
        sampled = sample_ortho(ortho, x - dx, y - dy)
        if sampled is not None and sampled[1].sum() >= MIN_ORTHO_SAMPLES:
            rgb_o, ok = sampled
            from_ortho = sensor_colour(
                to_lab(rgb_o[ok]), classify_ortho_lab, facets[ok], roof.xyz[ok, :2], low
            )
    return fuse(lidar, from_ortho)
