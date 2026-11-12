"""Contornos de edifícios a partir do ortofoto, guiados pelo LiDAR.

O LiDAR diz *onde* há um edifício e a que altura estão os telhados; o ortofoto
(25 cm) diz *onde ficam as arestas*. Para cada semente (mancha LiDAR de classe 6):

1. **Referencial:** tudo no referencial do ortofoto (= o do OSM, desenhado sobre
   imagem). O LiDAR é deslocado pelo desvio ortofoto ↔ LiDAR da quadrícula,
   estimado por correlação das arestas dos dois (:func:`estimate_ortho_offset`).
2. **Orientação dominante:** histograma das direções do gradiente (ortofoto e alturas)
   numa faixa à volta da semente, módulo 90°.
3. **Polígono inicial:** contorno da semente, simplificado e esquadrado nessa
   orientação.
4. **Ajuste das arestas:** cada lado desliza ao longo da sua normal (±2 m) até à
   posição com mais aresta no ortofoto *e* queda de altura no LiDAR (dentro alto,
   fora baixo). A queda de altura evita colar às sombras projetadas no chão.
5. **Divisão por alturas:** telhados separados por degraus (> ``STEP_M``) dão
   edifícios distintos e contíguos, com a parede comum partilhada.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import rasterio
import shapely
from rasterio.features import rasterize, shapes
from rasterio.transform import from_origin
from rasterio.windows import from_bounds
from scipy import fft, ndimage
from shapely.geometry import LineString, MultiPolygon, Polygon, shape
from shapely.geometry.base import BaseGeometry
from shapely.geometry.polygon import orient
from shapely.ops import split

from osm3d.geospatial.point_clouds import PointSet
from osm3d.geospatial.raster import DtmMosaic

log = logging.getLogger(__name__)

RES_M = 0.25  # grelha de trabalho = resolução do ORTOS
HEIGHT_RES_M = 0.5  # grelha das alturas LiDAR (= MDT)
WINDOW_MARGIN_M = 4.0
SNAP_RANGE_M = 2.0
SNAP_STEP_M = 0.125
SNAP_PROBES_M = (0.25, 0.5, 0.75)  # distâncias da queda de altura, de cada lado da aresta
# Peso da aresta do ortofoto sem queda de altura no LiDAR (sombras, marcas no telhado).
SHADOW_EDGE_WEIGHT = 0.25
MIN_SNAP_EDGE_M = 1.0
SQUARE_THRESHOLD_DEG = 20.0
STEP_M = 0.9  # degrau de altura entre células de 0,5 m que separa telhados
MIN_ROOF_REGION_M2 = 15.0
MIN_LEVEL_DIFF_M = 1.0  # diferença mínima de altura entre dois telhados separados
MAX_OFFSET_M = 4.0
NDVI_VEGETATION = 0.3  # telhados e chão construído ficam abaixo (P90 dos edifícios OSM ≈ 0)
GAP_FILL_M = 1.5  # folga máxima preenchida entre edifícios contíguos da mesma mancha
SHARED_ORIENTATION_DEG = 12.0  # peças com orientação próxima da mancha usam a da mancha


# ------------------------------------------------------------------ ortofoto


class OrthoMosaic:
    """Ortofotos (R, G, B, NIR) lidas por janelas, na grelha de 25 cm."""

    def __init__(self, paths: list[Path]) -> None:
        # Mais recentes primeiro: a primeira que cobre a janela é a usada.
        self._datasets = [rasterio.open(p) for p in sorted(paths, reverse=True)]

    def __enter__(self) -> OrthoMosaic:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        for ds in self._datasets:
            ds.close()

    def covers(self, bounds: tuple[float, float, float, float]) -> bool:
        return any(_contains(ds.bounds, bounds) for ds in self._datasets)

    def read(self, bounds: tuple[float, float, float, float]) -> np.ndarray | None:
        """(4, H, W) float32 — R, G, B, NIR — da janela (já alinhada à grelha)."""
        for ds in self._datasets:
            if not _contains(ds.bounds, bounds):
                continue
            win = from_bounds(*bounds, transform=ds.transform).round_offsets().round_lengths()
            n = min(ds.count, 4)
            data = ds.read(list(range(1, n + 1)), window=win).astype(np.float32)
            if n < 4:  # sem NIR: NDVI fica 0 (sem filtro de vegetação)
                data = np.concatenate([data, data[:1]] if n >= 3 else [data] * 4)[:4]
            return np.asarray(data, dtype=np.float32)
        return None


def _contains(b: object, bounds: tuple[float, float, float, float]) -> bool:
    left, bottom, right, top = b.left, b.bottom, b.right, b.top  # type: ignore[attr-defined]
    return bool(
        left <= bounds[0] and bottom <= bounds[1] and right >= bounds[2] and top >= bounds[3]
    )


def snap_bounds(
    bounds: tuple[float, float, float, float], res: float = RES_M
) -> tuple[float, float, float, float]:
    x0, y0, x1, y1 = bounds
    return (
        math.floor(x0 / res) * res,
        math.floor(y0 / res) * res,
        math.ceil(x1 / res) * res,
        math.ceil(y1 / res) * res,
    )


# ------------------------------------------------------------- alturas LiDAR


@dataclass
class HeightGrid:
    """Altura acima do terreno (máximo por célula), no referencial do LiDAR.

    ``h``: NaN sem pontos. ``occ``: células com pontos de edifício (classe 6).
    """

    x0: float
    y1: float
    res: float
    h: np.ndarray
    occ: np.ndarray

    def sample(
        self, x: np.ndarray, y: np.ndarray, offset: tuple[float, float], fill: float = 0.0
    ) -> np.ndarray:
        """Alturas em coordenadas do ortofoto (vizinho mais próximo)."""
        col = np.floor((x + offset[0] - self.x0) / self.res).astype(int)
        row = np.floor((self.y1 - (y + offset[1])) / self.res).astype(int)
        ok = (row >= 0) & (row < self.h.shape[0]) & (col >= 0) & (col < self.h.shape[1])
        out = np.full(x.shape, fill, dtype=np.float32)
        v = self.h[row[ok], col[ok]]
        out[ok] = np.where(np.isnan(v), fill, v)
        return out


def height_grid(
    points: PointSet,
    dtm: DtmMosaic,
    bounds: tuple[float, float, float, float],
    res: float = HEIGHT_RES_M,
) -> HeightGrid:
    """Grelha de alturas dos pontos de edifício (classe 6) acima do MDT."""
    x0, y0, x1, y1 = snap_bounds(bounds, res)
    nrow, ncol = round((y1 - y0) / res), round((x1 - x0) / res)
    pts = points.subset(points.classification == 6) if len(points) else points
    z = np.full((nrow, ncol), -np.inf)
    if len(pts):
        col = np.floor((pts.xyz[:, 0] - x0) / res).astype(int)
        row = np.floor((y1 - pts.xyz[:, 1]) / res).astype(int)
        ok = (row >= 0) & (row < nrow) & (col >= 0) & (col < ncol)
        np.maximum.at(z, (row[ok], col[ok]), pts.xyz[ok, 2])
    occ = np.isfinite(z)
    gx, gy, gz = dtm.cells_in(shapely.box(x0, y0, x1, y1))
    ground = np.full((nrow, ncol), np.nan)
    if gz.size:
        c = np.floor((gx - x0) / res).astype(int)
        r = np.floor((y1 - gy) / res).astype(int)
        ok = (r >= 0) & (r < nrow) & (c >= 0) & (c < ncol)
        ground[r[ok], c[ok]] = gz[ok]
        if np.isnan(ground).any():
            idx = ndimage.distance_transform_edt(
                np.isnan(ground), return_distances=False, return_indices=True
            )
            ground = ground[tuple(idx)]
    h = fill_scan_gaps(np.where(occ, z - ground, np.nan).astype(np.float32))
    return HeightGrid(x0, y1, res, h, ~np.isnan(h))


def fill_scan_gaps(h: np.ndarray, passes: int = 2, min_neighbours: int = 4) -> np.ndarray:
    """Preenche falhas estreitas entre linhas de varrimento (células sem pontos).

    Uma célula vazia com ≥ ``min_neighbours`` vizinhas (3×3) com altura recebe a média
    delas; ``passes`` repetições fecham falhas até ~1 m. Sem isto, as riscas vazias
    partem as águas do telhado em pedaços e escondem os degraus entre telhados.
    """
    h = h.copy()
    kernel = np.ones((3, 3), np.float32)
    for _ in range(passes):
        valid = ~np.isnan(h)
        count = ndimage.convolve(valid.astype(np.float32), kernel, mode="constant")
        total = ndimage.convolve(np.where(valid, h, 0.0), kernel, mode="constant")
        fill = ~valid & (count >= min_neighbours)
        if not fill.any():
            break
        h[fill] = total[fill] / count[fill]
    return h


# ------------------------------------------------ desvio ortofoto ↔ LiDAR


def _grad(a: np.ndarray, sigma: float) -> tuple[np.ndarray, np.ndarray]:
    """Gradiente em coordenadas do mundo (gx para este, gy para norte)."""
    s = ndimage.gaussian_filter(a, sigma)
    return ndimage.sobel(s, 1), -ndimage.sobel(s, 0)


def estimate_ortho_offset(
    grid: HeightGrid,
    ortho: OrthoMosaic,
    bounds: tuple[float, float, float, float],
    max_shift_m: float = MAX_OFFSET_M,
) -> tuple[float, float] | None:
    """Desvio (dx, dy) tal que ``LiDAR ≈ ortofoto + (dx, dy)``.

    Correlação (FFT) das arestas das alturas com as do ortofoto, só junto aos
    edifícios (pontos de classe 6). ``None`` sem ortofoto ou sem edifícios.
    """
    b = snap_bounds(bounds)
    if not ortho.covers(b):
        return None
    img = ortho.read(b)
    if img is None:
        return None
    gray = img[:3].mean(0)
    n_r, n_c = gray.shape
    xs = b[0] + (np.arange(n_c) + 0.5) * RES_M
    ys = b[3] - (np.arange(n_r) + 0.5) * RES_M
    X, Y = np.meshgrid(xs, ys)
    h = grid.sample(X.ravel(), Y.ravel(), (0.0, 0.0)).reshape(gray.shape)
    occ = h > 1.5
    if occ.sum() < 400:
        return None
    near = ndimage.binary_dilation(occ, iterations=4)

    def edges(a: np.ndarray) -> np.ndarray:
        gx, gy = _grad(a, 1.0)
        e = np.hypot(gx, gy) * near
        return np.asarray((e - e.mean()) / (e.std() or 1.0))

    el, eo = edges(h), edges(gray)
    cc = np.real(fft.ifft2(fft.fft2(el) * np.conj(fft.fft2(eo))))
    cc = fft.fftshift(cc)
    cy, cx = n_r // 2, n_c // 2
    L = int(max_shift_m / RES_M)
    win = cc[cy - L : cy + L + 1, cx - L : cx + L + 1]
    iy, ix = np.unravel_index(int(win.argmax()), win.shape)
    # Pico em (iy, ix): as arestas LiDAR estão (ix − L) colunas a este do ortofoto.
    return ((ix - L) * RES_M, -(iy - L) * RES_M)


# ----------------------------------------------------------- janela local


@dataclass
class Patch:
    """Janela do ortofoto à volta de uma semente, com as alturas reamostradas."""

    bounds: tuple[float, float, float, float]
    gx: np.ndarray  # gradiente do ortofoto (para este)
    gy: np.ndarray  # gradiente do ortofoto (para norte)
    h: np.ndarray  # altura acima do terreno (0 sem pontos de edifício)
    has: np.ndarray  # célula com pontos de edifício
    ndvi: np.ndarray
    edge_scale: float
    rgb: np.ndarray | None = None  # (3, H, W), para comparar a cor das telhas

    @property
    def transform(self):  # type: ignore[no-untyped-def]
        return from_origin(self.bounds[0], self.bounds[3], RES_M, RES_M)

    def _rc(self, x: np.ndarray, y: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        return (self.bounds[3] - y) / RES_M - 0.5, (x - self.bounds[0]) / RES_M - 0.5

    def interp(self, a: np.ndarray, x: np.ndarray, y: np.ndarray, order: int = 1) -> np.ndarray:
        r, c = self._rc(x, y)
        return np.asarray(ndimage.map_coordinates(a, [r, c], order=order, mode="nearest"))


def make_patch(
    seed: BaseGeometry, ortho: OrthoMosaic, grid: HeightGrid, offset: tuple[float, float]
) -> Patch | None:
    b = snap_bounds(seed.buffer(WINDOW_MARGIN_M).bounds)
    img = ortho.read(b)
    if img is None:
        return None
    r, g, bl, nir = img
    gray = (r + g + bl) / 3
    gx, gy = _grad(gray, 1.0)
    n_r, n_c = gray.shape
    xs = b[0] + (np.arange(n_c) + 0.5) * RES_M
    ys = b[3] - (np.arange(n_r) + 0.5) * RES_M
    X, Y = np.meshgrid(xs, ys)
    raw = grid.sample(X.ravel(), Y.ravel(), offset, fill=np.nan).reshape(gray.shape)
    has = ~np.isnan(raw)
    h = np.nan_to_num(raw, nan=0.0)
    ndvi = (nir - r) / np.maximum(nir + r, 1.0)
    return Patch(b, gx, gy, h, has, ndvi, edge_scale(gx, gy), np.stack([r, g, bl]))


def vegetation(patch: Patch) -> np.ndarray:
    """Células de vegetação no ortofoto (NDVI alto), sem ruído de pixel isolado."""
    return np.asarray(ndimage.binary_opening(patch.ndvi > NDVI_VEGETATION, iterations=1))


def edge_scale(gx: np.ndarray, gy: np.ndarray) -> float:
    """Escala das arestas da janela: o gradiente de uma aresta típica (P95)."""
    return max(float(np.percentile(np.hypot(gx, gy), 95)), 1.0)


# ------------------------------------------------------------- orientação


def smooth_heights(patch: Patch, sigma_px: float = 3.0) -> np.ndarray:
    """Alturas suavizadas só com as células com pontos (convolução normalizada).

    Tira a escada da grelha de 0,5 m (reamostrada a 0,25 m) sem misturar o chão.
    """
    w = ndimage.gaussian_filter(patch.has.astype(np.float32), sigma_px)
    s = ndimage.gaussian_filter(np.where(patch.has, patch.h, 0.0).astype(np.float32), sigma_px)
    return np.asarray(np.where(w > 1e-3, s / np.maximum(w, 1e-3), 0.0))


def _angle_hist(gx: np.ndarray, gy: np.ndarray, weight: np.ndarray) -> np.ndarray:
    bins = np.zeros(180)
    ang = np.mod(np.arctan2(gy, gx), np.pi / 2)
    k = (ang / (np.pi / 2) * 180).astype(int) % 180
    np.add.at(bins, k, weight)
    return np.asarray(ndimage.gaussian_filter1d(bins, 3, mode="wrap"))


def orientation_hist(patch: Patch, region: Polygon) -> np.ndarray:
    """Histograma (180 classes em [0°, 90°)) das direções das arestas de ``region``.

    - ortofoto: gradiente numa faixa de ±1,5 m à volta do contorno;
    - LiDAR: direção de maior declive das águas, no interior (perpendicular ou
      paralela à cumeeira, logo alinhada com as paredes). A grelha de alturas em
      escada puxaria para 0°/90°, por isso só se usa suavizada e longe dos bordos.
    """
    inside = rasterize([(region, 1)], out_shape=patch.h.shape, transform=patch.transform).astype(
        bool
    )
    band = ndimage.binary_dilation(inside, iterations=6) & ~ndimage.binary_erosion(
        inside, iterations=6
    )
    mag = np.hypot(patch.gx, patch.gy)[band] / patch.edge_scale
    hist = _angle_hist(patch.gx[band], patch.gy[band], mag**2)
    hist /= hist.sum() or 1.0
    core = ndimage.binary_erosion(inside & patch.has, iterations=4)
    if core.sum() * RES_M**2 >= 10.0:
        hs = smooth_heights(patch)
        hx, hy = _grad(hs, 0.0)
        slope = np.hypot(hx[core], hy[core])
        pitched = slope > 0.15 * np.median(slope[slope > 0]) if (slope > 0).any() else slope > 0
        if pitched.sum() * RES_M**2 >= 6.0:
            lh = _angle_hist(hx[core][pitched], hy[core][pitched], slope[pitched])
            hist += lh / (lh.sum() or 1.0)
    return hist


def dominant_orientation(patch: Patch, region: Polygon) -> float:
    """Ângulo dominante (rad, em [0, π/2)) das arestas de ``region``."""
    return float(np.argmax(orientation_hist(patch, region))) / 180 * (np.pi / 2)


def _angle_diff(a: float, b: float) -> float:
    """Diferença entre duas orientações módulo 90° (rad)."""
    d = abs(a - b) % (np.pi / 2)
    return float(min(d, np.pi / 2 - d))


# ----------------------------------------------------------- esquadria


def square_to(poly: Polygon, dom: float, threshold_deg: float = SQUARE_THRESHOLD_DEG) -> Polygon:
    """Lados a menos de ``threshold_deg`` de 0°/90° de ``dom`` ficam ortogonais."""
    thr = math.radians(threshold_deg)
    pts = np.asarray(orient(poly, 1.0).exterior.coords)[:-1]
    for _ in range(4):
        n = len(pts)
        if n < 4:
            break
        lines, snapped = [], []
        for i in range(n):
            a, b = pts[i], pts[(i + 1) % n]
            th = math.atan2(b[1] - a[1], b[0] - a[0])
            k = round((th - dom) / (math.pi / 2))
            th_s = dom + k * math.pi / 2
            ok = abs(th - th_s) <= thr
            d = (
                np.array([math.cos(th_s), math.sin(th_s)])
                if ok
                else (b - a) / (np.hypot(*(b - a)) or 1)
            )
            lines.append(((a + b) / 2, d))
            snapped.append(k % 4 if ok else None)
        # Lados consecutivos com a mesma direção: o vértice entre eles é redundante.
        keep = [
            i for i in range(n) if not (snapped[i] is not None and snapped[i - 1] == snapped[i])
        ]
        if len(keep) < n and len(keep) >= 3:
            pts = pts[keep]
            continue
        new = [_intersect(lines[i - 1], lines[i], pts[i]) for i in range(n)]
        pts = np.asarray(new)
    out = Polygon(pts)
    return out if out.is_valid and out.area > 0 else poly


Line = tuple[np.ndarray, np.ndarray]  # (ponto, direção unitária)


def _intersect(l1: Line, l2: Line, fallback: np.ndarray) -> np.ndarray:
    (p1, d1), (p2, d2) = l1, l2
    den = d1[0] * d2[1] - d1[1] * d2[0]
    if abs(den) < 0.05:
        # Lados (quase) paralelos: ponto médio das projeções do vértice antigo.
        q1 = p1 + d1 * float((fallback - p1) @ d1)
        q2 = p2 + d2 * float((fallback - p2) @ d2)
        return (q1 + q2) / 2
    t = ((p2[0] - p1[0]) * d2[1] - (p2[1] - p1[1]) * d2[0]) / den
    return np.asarray(p1 + t * d1)


def drop_short_steps(poly: Polygon, min_len: float) -> Polygon:
    """Remove degraus (pares de lados curtos) que mudam pouco a área."""
    pts = list(np.asarray(orient(poly, 1.0).exterior.coords)[:-1])
    changed = True
    while changed and len(pts) > 4:
        changed = False
        n = len(pts)
        lens = [float(np.hypot(*(pts[(i + 1) % n] - pts[i]))) for i in range(n)]
        order = np.argsort(lens)
        for i in order:
            if lens[i] >= min_len:
                break
            # Retirar os dois vértices do lado curto e unir os vizinhos por interseção.
            a, b = i, (i + 1) % n
            prev_l = (pts[a - 1], pts[a] - pts[a - 1])
            next_l = (pts[(b + 1) % n], pts[(b + 2) % n] - pts[(b + 1) % n])
            if np.hypot(*prev_l[1]) == 0 or np.hypot(*next_l[1]) == 0:
                continue
            p = _intersect(
                (prev_l[0], prev_l[1] / np.hypot(*prev_l[1])),
                (next_l[0], next_l[1] / np.hypot(*next_l[1])),
                (pts[a] + pts[b]) / 2,
            )
            trial = [q for k, q in enumerate(pts) if k not in (a, b)]
            ins = a if a < b else len(trial)
            trial.insert(min(ins, len(trial)), p)
            cand = Polygon(trial)
            if cand.is_valid and abs(cand.area - poly.area) <= 0.08 * poly.area:
                pts = list(np.asarray(orient(cand, 1.0).exterior.coords)[:-1])
                changed = True
                break
    out = Polygon(pts)
    return out if out.is_valid else poly


# -------------------------------------------------------- ajuste das arestas


def edge_scores(patch: Patch, s: np.ndarray, n: np.ndarray, offsets: np.ndarray) -> np.ndarray:
    """Pontuação de cada deslocamento ``d`` do lado (amostras ``s``) ao longo de ``n``.

    Aresta do ortofoto (gradiente na direção da normal) + queda de altura do LiDAR
    (dentro alto, fora baixo), com uma pequena penalização por se afastar.

    A aresta do ortofoto só conta por inteiro onde o LiDAR vê uma queda de altura: a
    borda de uma sombra projetada sobre um telhado (ou sobre o chão, longe da parede)
    é uma aresta forte na imagem mas não é parede.
    """
    q = s[None, :, :] + offsets[:, None, None] * n  # (m, k, 2)
    x, y = q[..., 0].ravel(), q[..., 1].ravel()
    # Queda de altura a várias distâncias: perfil triangular centrado no degrau (uma
    # só sonda dava um patamar com a largura da sonda).
    drop = np.zeros(q.shape[:2])
    for dist in SNAP_PROBES_M:
        p = dist * n
        hi = patch.interp(patch.h, x - p[0], y - p[1], order=0).reshape(q.shape[:2])
        ho = patch.interp(patch.h, x + p[0], y + p[1], order=0).reshape(q.shape[:2])
        drop += np.clip((hi - ho) / 2.5, -0.5, 1.0) / len(SNAP_PROBES_M)
    gx = patch.interp(patch.gx, x, y).reshape(q.shape[:2])
    gy = patch.interp(patch.gy, x, y).reshape(q.shape[:2])
    support = np.clip(SHADOW_EDGE_WEIGHT + drop, 0.0, 1.0)
    ortho = (np.abs(gx * n[0] + gy * n[1]) * support).mean(1) / patch.edge_scale
    # Saturação suave (sem teto plano: o pico fica na aresta).
    ortho_term = 2.0 * ortho / (1.0 + ortho)
    return np.asarray(ortho_term + 1.5 * drop.mean(1) - 0.05 * np.abs(offsets))


def snap_edges(patch: Patch, poly: Polygon, passes: int = 2) -> Polygon:
    """Desliza cada lado até à aresta mais forte (ortofoto + queda de altura)."""
    pts = np.asarray(orient(poly, 1.0).exterior.coords)[:-1]
    offsets = np.arange(-SNAP_RANGE_M, SNAP_RANGE_M + 1e-9, SNAP_STEP_M)
    for _ in range(passes):
        n = len(pts)
        lines: list[Line] = []
        for i in range(n):
            a, b = pts[i], pts[(i + 1) % n]
            length = float(np.hypot(*(b - a)))
            t = (b - a) / (length or 1.0)
            nrm = np.array([t[1], -t[0]])  # exterior (anel CCW)
            if length < MIN_SNAP_EDGE_M:
                lines.append(((a + b) / 2, t))
                continue
            k = max(2, int(length * 0.7 / RES_M))
            s = a + np.outer(np.linspace(0.15, 0.85, k), b - a)
            d = float(offsets[int(np.argmax(edge_scores(patch, s, nrm, offsets)))])
            lines.append(((a + b) / 2 + d * nrm, t))
        pts = np.asarray([_intersect(lines[i - 1], lines[i], pts[i]) for i in range(n)])
    out = Polygon(pts)
    return out if out.is_valid and out.area > 0 else poly


# ------------------------------------------------------------------ chanfros


def remove_chamfers(
    poly: Polygon, dom: float, patch: Patch | None = None, max_run_m: float | None = None
) -> Polygon:
    """Cantos chanfrados → cantos vivos.

    Uma sequência de lados oblíquos entre dois lados alinhados com ``dom`` é
    substituída pelo canto (lados perpendiculares) ou por um lado perpendicular
    (lados paralelos: degrau). Típico das manchas raster, cujos cantos ficam
    arredondados pelas operações morfológicas. Só se a sequência for curta (por
    omissão < 35 % do perímetro), a área mudar < 12 % e, com ``patch``, o LiDAR
    apoiar a mudança (paredes oblíquas verdadeiras ficam).
    """
    thr = math.radians(SQUARE_THRESHOLD_DEG)
    if max_run_m is None:
        max_run_m = 0.35 * poly.length
    pts = list(np.asarray(orient(poly, 1.0).exterior.coords)[:-1])
    area = poly.area
    changed = True
    while changed and len(pts) > 4:
        changed = False
        n = len(pts)
        info = []
        for i in range(n):
            v = pts[(i + 1) % n] - pts[i]
            th = math.atan2(v[1], v[0])
            k = round((th - dom) / (math.pi / 2))
            info.append((abs(th - (dom + k * math.pi / 2)) <= thr, k % 2, float(np.hypot(*v))))
        for i in range(n):
            if info[i][0] or not info[i - 1][0]:
                continue
            j, total = i, 0.0
            while not info[j % n][0] and total < max_run_m and j - i < n - 2:
                total += info[j % n][2]
                j += 1
            if not info[j % n][0] or total >= max_run_m:
                continue
            # Rodar: lado p = 0→1, lados oblíquos 1→…→r, lado q = r→r+1.
            rot = [pts[(i - 1 + m) % n] for m in range(n)]
            r = j - i + 1
            dp = rot[1] - rot[0]
            dq = rot[(r + 1) % n] - rot[r]
            if not np.hypot(*dp) or not np.hypot(*dq):
                continue
            dp, dq = dp / np.hypot(*dp), dq / np.hypot(*dq)
            if info[i - 1][1] != info[j % n][1]:
                new = [_intersect((rot[0], dp), (rot[r], dq), rot[1])]
            else:
                c = float(np.mean([rot[m] @ dp for m in range(1, r + 1)]))
                pa = rot[0] + dp * (c - float(rot[0] @ dp))
                den = float(dq @ dp)
                if abs(den) < 1e-6:
                    continue
                pb = rot[r] + dq * ((c - float(rot[r] @ dp)) / den)
                new = [pa, pb]
            trial = [rot[0], *new, *rot[r + 1 :]]
            if len(trial) < 3:
                continue
            cand = Polygon(trial)
            if (
                cand.is_valid
                and abs(cand.area - area) <= 0.12 * area
                and (patch is None or _supported(patch, Polygon(pts), cand))
            ):
                pts = list(np.asarray(orient(cand, 1.0).exterior.coords)[:-1])
                changed = True
                break
    out = Polygon(pts)
    return out if out.is_valid and out.area > 0 else poly


def coverage(patch: Patch, geom: BaseGeometry) -> float | None:
    """Fração das células de ``geom`` com pontos de edifício (``None`` se vazia)."""
    if geom.is_empty or geom.area < RES_M**2:
        return None
    m = rasterize([(geom, 1)], out_shape=patch.has.shape, transform=patch.transform).astype(bool)
    return float(patch.has[m].mean()) if m.any() else None


def vegetation_fraction(patch: Patch, geom: BaseGeometry) -> float:
    """Fração de ``geom`` com vegetação no ortofoto."""
    if geom.is_empty:
        return 0.0
    m = rasterize([(geom, 1)], out_shape=patch.h.shape, transform=patch.transform).astype(bool)
    return float(vegetation(patch)[m].mean()) if m.any() else 0.0


def _supported(patch: Patch, old: Polygon, new: Polygon) -> bool:
    """O LiDAR apoia a mudança? Área ganha tem telhado; área perdida não tem."""
    gained = coverage(patch, new.difference(old))
    lost = coverage(patch, old.difference(new))
    if gained is not None and new.difference(old).area > 1.0 and gained < 0.35:
        return False
    return not (lost is not None and old.difference(new).area > 1.0 and lost > 0.6)


# ----------------------------------------------------------------- contorno


def regularize(region: Polygon, dom: float, patch: Patch | None = None) -> Polygon:
    """Mancha raster → polígono simples, esquadrado e sem chanfros."""
    clean = Polygon(region.exterior).buffer(0)
    if isinstance(clean, MultiPolygon):
        clean = max(clean.geoms, key=lambda g: g.area)
    simple = clean.simplify(0.75, preserve_topology=True)
    if not isinstance(simple, Polygon) or simple.is_empty:
        simple = clean
    sq = square_to(simple, dom)
    sq = remove_chamfers(sq, dom, patch)
    sq = drop_short_steps(sq, 1.2)
    return orient(square_to(sq, dom), 1.0)


def outline(seed: Polygon, patch: Patch, dom: float | None = None) -> tuple[Polygon, float]:
    """Contorno ajustado ao ortofoto e orientação dominante (rad)."""
    if dom is None:
        dom = dominant_orientation(patch, seed)
    sq = regularize(seed, dom, patch)
    snapped = snap_edges(patch, sq)
    snapped = remove_chamfers(snapped, dom, patch)
    snapped = drop_short_steps(snapped, 0.8)
    if not snapped.is_valid or not 0.6 * sq.area <= snapped.area <= 1.6 * sq.area:
        snapped = sq
    return orient(remove_spikes(snapped), 1.0), dom


def remove_spikes(poly: Polygon, width_m: float = 1.2) -> Polygon:
    """Tira caudas e espinhos mais estreitos que ``width_m`` (abertura em esquadria)."""
    g = width_m / 2
    opened = _largest(poly.buffer(-g, join_style="mitre").buffer(g, join_style="mitre"))
    if opened is None or opened.area < 0.85 * poly.area:
        return poly
    simple = opened.simplify(0.05)
    return simple if isinstance(simple, Polygon) and simple.is_valid else opened


# --------------------------------------------- divisão em telhados contínuos


def segment_roofs(patch: Patch, mask: np.ndarray) -> tuple[np.ndarray, dict[int, float]]:
    """Partição de ``mask`` em superfícies de telhado contínuas (sem degraus).

    Cada superfície é um edifício: telhados separados por um degrau (> ``STEP_M``)
    são edifícios distintos e contíguos. Devolve (rótulos, altura P90 por rótulo).
    """
    h = ndimage.median_filter(patch.h, size=3)
    step = np.zeros_like(mask)
    for dr, dc in ((0, 2), (2, 0), (2, 2), (2, -2)):
        jump = np.abs(h - np.roll(h, (dr, dc), axis=(0, 1))) > STEP_M
        step |= jump | np.roll(jump, (-dr, -dc), axis=(0, 1))
    roof = mask & patch.has & ~step
    labels, k = ndimage.label(roof)
    if k == 0:
        return np.where(mask, 1, 0).astype(np.int32), {1: float(np.percentile(h[mask], 90))}
    sizes = ndimage.sum(roof, labels, range(1, k + 1)) * RES_M**2
    big = np.flatnonzero(sizes >= MIN_ROOF_REGION_M2) + 1
    if len(big) == 0:
        big = np.array([int(np.argmax(sizes)) + 1])
    seeds = np.where(np.isin(labels, big), labels, 0)
    _, (ri, ci) = ndimage.distance_transform_edt(seeds == 0, return_indices=True)
    part = np.where(mask, seeds[ri, ci], 0).astype(np.int32)

    # Vizinhas sem degrau real na fronteira comum (um degrau de ruído separou-as):
    # são a mesma superfície.
    pairs: dict[tuple[int, int], list[float]] = {}
    for dr, dc in ((0, 2), (2, 0)):
        a, b = part, np.roll(part, (dr, dc), axis=(0, 1))
        ok = (a > 0) & (b > 0) & (a != b) & patch.has & np.roll(patch.has, (dr, dc), (0, 1))
        diff = np.abs(h - np.roll(h, (dr, dc), axis=(0, 1)))
        for la, lb, d in zip(a[ok], b[ok], diff[ok], strict=True):
            pairs.setdefault((min(int(la), int(lb)), max(int(la), int(lb))), []).append(float(d))
    parent = {int(lab): int(lab) for lab in big}

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for (la, lb), ds in pairs.items():
        if len(ds) >= 8 and float(np.median(ds)) < 0.5 * STEP_M:
            parent[find(la)] = find(lb)
    lut = np.zeros(int(part.max()) + 1, np.int32)
    for lab in parent:
        lut[lab] = find(lab)
    roots = lut[part]
    levels = {
        int(lab): float(np.percentile(h[(roots == lab) & patch.has], 90))
        for lab in np.unique(roots)
        if lab and ((roots == lab) & patch.has).any()
    }
    return roots, levels


# ------------------------------------------------ divisão pelos planos do telhado

FLAT_SLOPE = 0.15  # declive (m/m) abaixo do qual a água é plana (~8,5°)
PLANE_SMOOTH_PX = 3.0  # suavização (0,75 m) antes do declive: tira a escada da grelha de 0,5 m
MIN_PLANE_M2 = 3.0
ENCLOSED_MAX_M2 = 30.0  # trapeiras, painéis, chaminés: absorvidos pelo telhado à volta
SMALL_SLOPED_M2 = 15.0  # água inclinada pequena sobre telhado plano (painéis): mesmo edifício
LEVEL_TOL_M = 0.4  # laró entre águas com beiral e cumeeira a esta tolerância: casa em L
THIN_PLANE_PX = 3  # faixa "plana" que desaparece com erosão de 0,75 m: é cumeeira


def roof_planes(patch: Patch, mask: np.ndarray, dom: float) -> np.ndarray:
    """Águas do telhado: componentes ligadas com a mesma classe de orientação.

    Classes: plana, ou a direção de maior declive mais próxima de ``dom`` + k·90°
    (as quatro águas de um telhado alinhado com as paredes). Cumeeiras, rincões e
    larós separam classes diferentes; águas da mesma classe ligadas são uma só.
    """
    hs = smooth_heights(patch, PLANE_SMOOTH_PX)
    gr, gc = np.gradient(hs, RES_M)
    sx, sy = gc, -gr  # declive para este e para norte
    slope = np.hypot(sx, sy)
    valid = ndimage.binary_erosion(mask & patch.has, iterations=2)
    aspect = np.arctan2(sy, sx) - dom
    cls = np.where(slope < FLAT_SLOPE, 0, 1 + np.mod(np.round(aspect / (np.pi / 2)), 4)).astype(
        np.int32
    )
    cls[~valid] = -1
    planes = np.zeros(mask.shape, np.int32)
    n = 0
    min_px = MIN_PLANE_M2 / RES_M**2
    for c in range(5):
        lab, k = ndimage.label(ndimage.binary_opening(cls == c, iterations=1))
        if not k:
            continue
        sizes = ndimage.sum(np.ones_like(lab), lab, range(1, k + 1))
        for i in np.flatnonzero(sizes >= min_px) + 1:
            n += 1
            planes[lab == i] = n
    return planes


def _pairs(lab: np.ndarray) -> dict[tuple[int, int], int]:
    """Comprimento da fronteira (em células) entre cada par de rótulos vizinhos."""
    out: dict[tuple[int, int], int] = {}
    for a, b in ((lab[:, :-1], lab[:, 1:]), (lab[:-1, :], lab[1:, :])):
        ok = (a > 0) & (b > 0) & (a != b)
        if not ok.any():
            continue
        lo, hi = np.minimum(a[ok], b[ok]), np.maximum(a[ok], b[ok])
        keys, counts = np.unique(np.stack([lo, hi], 1), axis=0, return_counts=True)
        for (x, y), cnt in zip(keys, counts, strict=True):
            out[(int(x), int(y))] = out.get((int(x), int(y)), 0) + int(cnt)
    return out


def _fill_nearest(lab: np.ndarray, mask: np.ndarray) -> np.ndarray:
    if not (lab > 0).any():
        return np.where(mask, 1, 0).astype(np.int32)
    _, (ri, ci) = ndimage.distance_transform_edt(lab == 0, return_indices=True)
    return np.where(mask, lab[ri, ci], 0).astype(np.int32)


def split_by_planes(patch: Patch, surface: np.ndarray, dom: float) -> np.ndarray:
    """Divide uma superfície contínua em edifícios pelos planos do telhado.

    Duas águas vizinhas são o mesmo edifício se a fronteira for **convexa** (cumeeira
    ou rincão: as duas descem a partir dela) e sem degrau, ou se forem ambas planas.
    Laró (as duas sobem), degrau, ou plano encostado a inclinado separam edifícios.
    Grupos pequenos quase rodeados por outro (trapeiras, painéis) juntam-se a ele.
    Devolve rótulos (1…n) sobre ``surface``.
    """
    planes = roof_planes(patch, surface, dom)
    if planes.max() < 2:
        return surface.astype(np.int32)
    full = _fill_nearest(planes, surface)
    hs = smooth_heights(patch, PLANE_SMOOTH_PX)
    gr, gc = np.gradient(hs, RES_M)
    sx, sy = gc, -gr
    rows, cols = np.indices(full.shape)
    info: dict[int, tuple[np.ndarray, np.ndarray, bool]] = {}
    for p in range(1, int(planes.max()) + 1):
        m = planes == p
        if not m.any():
            continue
        g = np.array([np.median(sx[m]), np.median(sy[m])])
        centre = np.array([cols[m].mean(), -rows[m].mean()])  # (x, y) em células
        info[p] = (g, centre, bool(np.hypot(*g) < FLAT_SLOPE))
    # (beiral, cumeeira) de cada água: P10 e P90 das alturas.
    level = {
        p: (
            float(np.percentile(patch.h[(planes == p) & patch.has], 10)),
            float(np.percentile(patch.h[(planes == p) & patch.has], 90)),
        )
        for p in info
        if ((planes == p) & patch.has).any()
    }
    info = {p: v for p, v in info.items() if p in level}
    # Águas com menos de ~1,5 m de largura (faixas ao longo de cumeeiras e rincões).
    thin = {
        p: not ndimage.binary_erosion(planes == p, iterations=THIN_PLANE_PX).any() for p in info
    }
    parent = {p: p for p in info}

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for (a, b), length in _pairs(full).items():
        if a not in info or b not in info or length < 4:
            continue
        # Fronteira: células de a encostadas a b.
        nb = ndimage.binary_dilation(full == b) & (full == a)
        if not nb.any():
            continue
        bc = np.array([cols[nb].mean(), -rows[nb].mean()])
        # Degrau na fronteira? (alturas de um lado e do outro, a 0,5 m)
        ring_a = ndimage.binary_dilation(nb, iterations=2) & (full == a) & patch.has
        ring_b = ndimage.binary_dilation(nb, iterations=3) & (full == b) & patch.has
        if not ring_a.any() or not ring_b.any():
            continue
        if abs(float(np.median(patch.h[ring_a])) - float(np.median(patch.h[ring_b]))) > 0.6:
            continue
        ga, ca, flat_a = info[a]
        gb, cb, flat_b = info[b]
        if flat_a and flat_b:
            parent[find(a)] = find(b)
            continue
        if flat_a != flat_b:
            # Plano + inclinado pequeno (painéis, claraboias), ou faixa "plana" estreita
            # (a cumeeira, onde o declive suavizado passa por zero): o mesmo telhado.
            flat, sloped = (a, b) if flat_a else (b, a)
            if (planes == sloped).sum() * RES_M**2 < SMALL_SLOPED_M2 or thin[flat]:
                parent[find(a)] = find(b)
            continue
        # A mesma água partida pela classificação (orientação perto de 45° de ``dom``).
        if float(np.hypot(*(ga - gb))) < 0.35 * max(float(np.hypot(*ga)), float(np.hypot(*gb))):
            parent[find(a)] = find(b)
            continue
        da, db = ca - bc, cb - bc
        da /= np.hypot(*da) or 1.0
        db /= np.hypot(*db) or 1.0
        # Convexa: ao afastar-se da fronteira, as duas águas descem.
        if float(ga @ da) < 0 and float(gb @ db) < 0:
            parent[find(a)] = find(b)
            continue
        # Laró (côncava): casa em L se cumeeiras e beirais estão à mesma altura.
        ha, hb = level[a], level[b]
        if abs(ha[0] - hb[0]) < LEVEL_TOL_M and abs(ha[1] - hb[1]) < LEVEL_TOL_M:
            parent[find(a)] = find(b)

    lut = np.zeros(int(full.max()) + 1, np.int32)
    for p in info:
        lut[p] = find(p)
    groups = np.where(surface, lut[full], 0)
    return _absorb_small(groups, surface)


def _absorb_small(groups: np.ndarray, surface: np.ndarray) -> np.ndarray:
    """Grupos pequenos ou rodeados por um vizinho juntam-se a esse vizinho."""
    changed = True
    while changed:
        changed = False
        ids, counts = np.unique(groups[groups > 0], return_counts=True)
        if len(ids) < 2:
            break
        area = dict(zip(ids.tolist(), (counts * RES_M**2).tolist(), strict=True))
        pairs = _pairs(groups)
        perim = {
            g: int((ndimage.binary_dilation(groups == g) & ~(groups == g) & surface).sum())
            for g in area
        }
        for g in sorted(area, key=lambda q: area[q]):
            shared = {(b if a == g else a): n for (a, b), n in pairs.items() if g in (a, b)}
            if not shared:
                continue
            best = max(shared, key=lambda q: shared[q])
            small = area[g] < MIN_ROOF_REGION_M2
            enclosed = (
                area[g] < ENCLOSED_MAX_M2
                and area[best] > area[g]
                and (shared[best] >= 0.6 * max(perim[g], 1))
            )
            if small or enclosed:
                groups[groups == g] = best
                changed = True
                break
    # Rótulos consecutivos.
    out = np.zeros_like(groups)
    for i, g in enumerate(np.unique(groups[groups > 0]), start=1):
        out[groups == g] = i
    return out


# ---------------------------------------------------- casas em banda (empenas)

ROW_MIN_PIECE_M2 = 25.0  # cada casa resultante de um corte
ROW_MIN_LENGTH_M = 8.0  # comprimento ao longo da cumeeira para tentar cortar
ROW_STRIP_M = (0.5, 2.5)  # faixas de cada lado da linha para comparar cor e cumeeira
ROW_EDGE_STRONG = 0.5  # gradiente (em escalas de aresta) que conta como "aresta"
ROW_MAX_DEPTH = 6


def _piece_level(patch: Patch, piece: Polygon, default: float) -> float:
    m = rasterize([(piece, 1)], out_shape=patch.h.shape, transform=patch.transform).astype(bool)
    m &= patch.has
    return float(np.percentile(patch.h[m], 90)) if m.any() else default


def split_rows(region: Polygon, patch: Patch, depth: int = 0) -> list[Polygon]:
    """Corta casas em banda pelas empenas comuns.

    Num telhado contínuo (sem degrau nem laró) de casas encostadas, a separação é
    uma linha perpendicular à cumeeira que atravessa o telhado todo. Procura-se essa
    linha ao longo da cumeeira com três sinais:

    - aresta no ortofoto contínua (fração da linha com gradiente forte);
    - mudança de cor da telha entre as faixas de cada lado;
    - salto da cumeeira (P95 das alturas de cada lado).

    Só em telhados inclinados (a cumeeira dá a direção); telhados planos ficam.
    """
    if depth >= ROW_MAX_DEPTH or patch.rgb is None or region.area < 2 * ROW_MIN_PIECE_M2:
        return [region]
    m = rasterize([(region, 1)], out_shape=patch.h.shape, transform=patch.transform).astype(bool)
    core = ndimage.binary_erosion(m & patch.has, iterations=2)
    if core.sum() < 50:
        return [region]
    hs = smooth_heights(patch, PLANE_SMOOTH_PX)
    gr, gc = np.gradient(hs, RES_M)
    sx, sy = gc, -gr
    slope = np.hypot(sx, sy)
    sloped = core & (slope > FLAT_SLOPE)
    if sloped.sum() < 0.4 * core.sum():
        return [region]
    ang = np.arctan2(sy[sloped], sx[sloped])
    z = np.sum(slope[sloped] * np.exp(2j * ang))
    a = float(np.angle(z)) / 2  # eixo do declive (perpendicular à cumeeira)
    er = np.array([-math.sin(a), math.cos(a)])  # ao longo da cumeeira
    rows, cols = np.nonzero(m)
    x0, _, _, y1 = patch.bounds
    X = x0 + (cols + 0.5) * RES_M
    Y = y1 - (rows + 0.5) * RES_M
    U = X * er[0] + Y * er[1]
    u0, u1 = np.percentile(U, [1, 99])
    if u1 - u0 < ROW_MIN_LENGTH_M:
        return [region]
    # Aresta de cor (maior gradiente dos três canais): duas telhas com o mesmo brilho
    # e cor diferente não têm aresta no cinzento.
    chans = [_grad(ch, 1.0) for ch in patch.rgb]
    mag = np.max([np.hypot(gx, gy) for gx, gy in chans], axis=0)
    along = np.max([np.abs(gx * er[0] + gy * er[1]) for gx, gy in chans], axis=0)
    edge = along[rows, cols] / max(float(np.percentile(mag, 95)), 1.0)
    rgb = patch.rgb[:, rows, cols].T
    h = np.where(patch.has[rows, cols], patch.h[rows, cols], np.nan)
    best: tuple[float, float] | None = None
    lo, hi = ROW_STRIP_M
    min_side = math.sqrt(ROW_MIN_PIECE_M2)
    for c in np.arange(u0 + min_side, u1 - min_side, 0.25):
        line = np.abs(U - c) < 0.2
        if line.sum() < 8:
            continue
        strong = float((edge[line] > ROW_EDGE_STRONG).mean())
        mid, half = (lo + hi) / 2, (hi - lo) / 2
        left = np.abs(U - (c - mid)) < half
        right = np.abs(U - (c + mid)) < half
        if left.sum() < 20 or right.sum() < 20:
            continue
        colour = float(np.linalg.norm(rgb[left].mean(0) - rgb[right].mean(0)))
        hl, hr = h[left], h[right]
        if np.isnan(hl).all() or np.isnan(hr).all():
            continue
        ridge = abs(float(np.nanpercentile(hl, 95)) - float(np.nanpercentile(hr, 95)))
        if not (
            (strong >= 0.6 and colour >= 15)
            or (ridge >= 0.35 and strong >= 0.4)
            or (colour >= 60 and strong >= 0.3)
        ):
            continue
        score = strong + colour / 40 + ridge
        if best is None or score > best[0]:
            best = (score, float(c))
    if best is None:
        return [region]
    c = best[1]
    ea = np.array([math.cos(a), math.sin(a)])
    # Ponto da linha junto à região (U = c), e a linha ao longo do declive.
    rc = np.asarray(region.centroid.coords[0])
    centre = c * er + float(rc @ ea) * ea
    reach = 2 * math.sqrt(region.area) + 100.0
    cut = LineString([tuple(centre - reach * ea), tuple(centre + reach * ea)])
    # A linha pode partir uma região irregular em vários pedaços: junta-os por lado.
    sides: dict[bool, list[Polygon]] = {True: [], False: []}
    for g in split(region, cut).geoms:
        if isinstance(g, Polygon):
            sides[float(np.asarray(g.centroid.coords[0]) @ er) > c].append(g)
    pieces = [_largest(shapely.union_all(v)) for v in sides.values() if v]
    if len(pieces) != 2 or any(p is None or p.area < ROW_MIN_PIECE_M2 for p in pieces):
        return [region]
    return [q for p in pieces if p is not None for q in split_rows(p, patch, depth + 1)]


def _region_polygon(part: np.ndarray, lab: int, patch: Patch) -> Polygon | None:
    # Abertura de 0,5 m: tira espinhos e pontes finas herdados da mancha.
    m = ndimage.binary_opening(part == lab, iterations=2).astype(np.uint8)
    polys = [shape(g) for g, v in shapes(m, mask=m == 1, transform=patch.transform) if v == 1]
    return max(polys, key=lambda p: p.area) if polys else None


# ------------------------------------------------- forma pelo ortofoto (segmentos)

SP_SCALE = 100.0  # Felzenszwalb: tamanho típico dos segmentos (águas, alpendres)
SP_SIGMA = 0.8
SP_MIN_PX = 40  # 2,5 m²
SP_MAJORITY = 0.5
SP_REACH_PX = 6  # um segmento só acrescenta até 1,5 m para fora da mancha LiDAR


def superpixels(patch: Patch) -> np.ndarray:
    """Segmentos de cor homogénea do ortofoto (águas, alpendres, sombras, chão)."""
    from skimage.segmentation import felzenszwalb

    if patch.rgb is None:
        return np.zeros(patch.h.shape, np.int64)
    img = np.moveaxis(np.clip(patch.rgb / 255.0, 0, 1), 0, -1)
    return np.asarray(felzenszwalb(img, scale=SP_SCALE, sigma=SP_SIGMA, min_size=SP_MIN_PX))


def refine_with_superpixels(patch: Patch, part: np.ndarray) -> np.ndarray:
    """Ajusta a partição do LiDAR aos segmentos do ortofoto.

    Cada segmento vai inteiro para a peça que cobre a maior parte dele, ou sai da
    partição se a maior parte não tiver telhado no LiDAR (sombra, chão, fachada
    vista de lado). As fronteiras passam a seguir os limites visíveis dos telhados;
    o LiDAR decide o que é telhado e de que edifício. Sem crescer mais de 1,5 m.
    """
    if patch.rgb is None or not (part > 0).any():
        return part
    seg = superpixels(patch)
    n_lab = int(part.max()) + 1
    key = seg.ravel() * n_lab + part.ravel()
    counts = np.bincount(key, minlength=(int(seg.max()) + 1) * n_lab).reshape(-1, n_lab)
    total = counts.sum(1)
    best = counts[:, 1:].argmax(1) + 1
    best_frac = counts[np.arange(len(best)), best] / np.maximum(total, 1)
    out_frac = counts[:, 0] / np.maximum(total, 1)
    assign = np.where(best_frac >= SP_MAJORITY, best, np.where(out_frac >= SP_MAJORITY, 0, -1))
    new = assign[seg]
    keep = new < 0  # segmentos sem maioria: fica o LiDAR, pixel a pixel
    new = np.where(keep, part, new)
    reach = ndimage.binary_dilation(part > 0, iterations=SP_REACH_PX)
    new = np.where(reach & ~vegetation(patch), new, 0)
    # Peças que desapareceram ou ficaram em pedaços minúsculos: volta o LiDAR.
    for lab in range(1, n_lab):
        if (part == lab).any() and (new == lab).sum() < 0.5 * (part == lab).sum():
            new = np.where((part == lab) & (new == 0), lab, new)
    return np.asarray(new, dtype=np.int32)


def buildings_from_seed(seed: Polygon, patch: Patch) -> list[Polygon]:
    """Edifícios (contíguos, sem sobreposição) dentro de uma mancha LiDAR."""
    mask = rasterize([(seed, 1)], out_shape=patch.h.shape, transform=patch.transform).astype(bool)
    # Vegetação (árvores sobre o telhado ou mal classificadas como edifício) sai da
    # semente: fragmentos só de vegetação desaparecem, casas ficam só com o telhado.
    mask &= ~vegetation(patch)
    seed_dom = dominant_orientation(patch, seed)
    surfaces, _ = segment_roofs(patch, mask)
    # Cada superfície contínua (sem degraus) ainda pode ter vários edifícios: divide-se
    # pelos planos do telhado.
    part = np.zeros_like(surfaces)
    n = 0
    for s in np.unique(surfaces[surfaces > 0]):
        # Classes de orientação das águas pela orientação da mancha (a de cada
        # superfície falha em superfícies com braços oblíquos).
        sub = split_by_planes(patch, surfaces == s, seed_dom)
        for k in range(1, int(sub.max()) + 1):
            n += 1
            part[sub == k] = n
    # A forma vem do ortofoto: cada segmento de cor homogénea vai para a peça do LiDAR
    # que o cobre (ou sai, se for sombra, chão ou fachada).
    part = refine_with_superpixels(patch, part)
    levels = {
        int(lab): float(np.percentile(patch.h[(part == lab) & patch.has], 90))
        for lab in np.unique(part[part > 0])
        if ((part == lab) & patch.has).any()
    }
    out: list[Polygon] = []
    taken: BaseGeometry | None = None
    # Os telhados mais altos primeiro: o beiral do mais alto cobre a parede comum.
    regions: list[tuple[float, Polygon]] = []
    for lab, level in levels.items():
        region = _region_polygon(part, lab, patch)
        if region is None or region.area < MIN_ROOF_REGION_M2:
            continue
        # Casas em banda com o mesmo telhado: corte pelas empenas (ortofoto + cumeeira).
        for piece in split_rows(region, patch):
            regions.append((_piece_level(patch, piece, level), piece))
    for _, region in sorted(regions, key=lambda q: -q[0]):
        # Orientação comum à mancha se a da peça for parecida: paredes encostadas
        # ficam paralelas (sem cantos cortados entre vizinhos).
        dom = dominant_orientation(patch, region)
        if _angle_diff(dom, seed_dom) < math.radians(SHARED_ORIENTATION_DEG):
            dom = seed_dom
        poly: Polygon | None
        poly, _ = outline(region, patch, dom)
        if taken is not None:
            # Vértices a < 0,6 m da parede do vizinho passam para ela (fecha folgas
            # estreitas sem acrescentar área por fora); depois recorta a sobreposição.
            poly = _largest(shapely.snap(poly, taken, 0.6).buffer(0).difference(taken))
            if poly is None:
                continue
            # Faixas finas (< 1 m) que sobram ao lado do vizinho.
            poly = _largest(poly.buffer(-0.5, join_style="mitre").buffer(0.5, join_style="mitre"))
            poly = None if poly is None else _largest(poly.difference(taken))
            poly = None if poly is None else fill_gaps(poly, taken)
        if poly is None or poly.area < MIN_ROOF_REGION_M2:
            continue
        poly = orient(poly, 1.0)
        out.append(poly)
        taken = poly if taken is None else taken.union(poly)
    return out


# ------------------------------------------------ alpendres: partes, não edifícios

LEAN_TO_MIN_SHARED = 0.3  # fração do perímetro do alpendre encostada ao principal
LEAN_TO_MIN_DROP_M = 0.5  # o alpendre é mais baixo que o principal
LEAN_TO_MIN_ALIGNED = 0.6  # fração das células inclinadas com o declive dominante


def _roof_of(patch: Patch, poly: Polygon) -> tuple[float, np.ndarray, float] | None:
    """(altura P90, declive mediano (dz/dx, dz/dy), fração alinhada com ele)."""
    m = rasterize([(poly, 1)], out_shape=patch.h.shape, transform=patch.transform).astype(bool)
    core = ndimage.binary_erosion(m & patch.has, iterations=2)
    if core.sum() < 20:
        return None
    hs = smooth_heights(patch, PLANE_SMOOTH_PX)
    gr, gc = np.gradient(hs, RES_M)
    sx, sy = gc[core], -gr[core]
    g = np.array([np.median(sx), np.median(sy)])
    norm = float(np.hypot(*g))
    if norm < FLAT_SLOPE:
        return float(np.percentile(patch.h[m & patch.has], 90)), g, 0.0
    cos = (sx * g[0] + sy * g[1]) / np.maximum(np.hypot(sx, sy) * norm, 1e-9)
    return float(np.percentile(patch.h[m & patch.has], 90)), g, float((cos > 0.87).mean())


def group_lean_tos(patch: Patch, pieces: list[Polygon]) -> list[list[Polygon]]:
    """Junta cada alpendre ao edifício a que se encosta: um edifício com partes.

    Alpendre (anexo de uma água): mais baixo que o vizinho, encostado ao longo de um
    lado dele (≥ 30 % do seu perímetro) e com uma água só que **desce para longe**
    dele. Fica como ``building:part`` desse edifício. Volumes com telhado próprio
    (duas águas, quatro águas, plano) continuam edifícios contíguos.
    Devolve grupos de partes; a primeira de cada grupo é o volume principal.
    """
    if len(pieces) < 2:
        return [[p] for p in pieces]
    roofs = [_roof_of(patch, p) for p in pieces]
    parent = list(range(len(pieces)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for i, (p, r) in enumerate(zip(pieces, roofs, strict=True)):
        if r is None or r[2] < LEAN_TO_MIN_ALIGNED:
            continue
        level, grad, _ = r
        best: tuple[float, int] | None = None
        for j, (q, rq) in enumerate(zip(pieces, roofs, strict=True)):
            if j == i or rq is None or rq[0] - level < LEAN_TO_MIN_DROP_M:
                continue
            shared = p.buffer(0.3).intersection(q.boundary).length
            if shared < LEAN_TO_MIN_SHARED * p.length / 2:
                continue
            # Desce para longe do vizinho: o declive aponta para o vizinho.
            away = np.asarray(p.centroid.coords[0]) - np.asarray(
                p.buffer(0.3).intersection(q.boundary).centroid.coords[0]
            )
            if float(grad @ away) >= 0:
                continue
            if best is None or shared > best[0]:
                best = (shared, j)
        if best is not None:
            parent[find(i)] = find(best[1])
    groups: dict[int, list[int]] = {}
    for i in range(len(pieces)):
        groups.setdefault(find(i), []).append(i)
    levels = [r[0] if r is not None else 0.0 for r in roofs]
    out = []
    for members in groups.values():
        members.sort(key=lambda k: -levels[k])  # principal primeiro: o mais alto
        out.append([pieces[k] for k in members])
    return out


def fill_gaps(poly: Polygon, taken: BaseGeometry, max_gap_m: float = GAP_FILL_M) -> Polygon:
    """Encosta ``poly`` aos vizinhos: folgas < ``max_gap_m`` entre os dois passam a ``poly``.

    Peças da mesma mancha LiDAR são contíguas (o LiDAR viu telhado sem interrupção);
    uma folga entre elas vem do ajuste das arestas (sombras, beirais). O fecho
    morfológico da união preenche só os corredores estreitos entre as peças, sem
    alargar as paredes exteriores.
    """
    g = max_gap_m / 2
    closed = poly.union(taken).buffer(g, join_style="mitre").buffer(-g, join_style="mitre")
    gap = closed.difference(poly).difference(taken)
    add = [
        c
        for c in getattr(gap, "geoms", [gap])
        if isinstance(c, Polygon)
        and c.area > 0.05
        and c.distance(poly) < 1e-3
        and c.distance(taken) < 1e-3
    ]
    if not add:
        return poly
    merged = _largest(shapely.union_all([poly, *add]).difference(taken))
    return merged if merged is not None else poly


def _largest(g: BaseGeometry) -> Polygon | None:
    if g.is_empty:
        return None
    polys = [p for p in getattr(g, "geoms", [g]) if isinstance(p, Polygon)]
    return max(polys, key=lambda p: p.area) if polys else None
