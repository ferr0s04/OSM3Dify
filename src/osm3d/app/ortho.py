"""Tiles XYZ (Web Mercator) das ortofotos descarregadas, para fundo do mapa.

As ortofotos da DGT são COG (GeoTIFF em blocos, com overviews) em EPSG:3763. Para cada
tile lê-se só a janela necessária, ao nível de overview adequado, e reprojeta-se para
256 × 256 px em EPSG:3857. As folhas mais recentes ficam por cima das antigas; fora das
folhas o tile é transparente (e sem folhas devolve-se ``None``: 204 na API).
"""

from __future__ import annotations

import io
import logging
import math
import re
import threading
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from affine import Affine
from PIL import Image
from rasterio.enums import ColorInterp, Resampling
from rasterio.errors import WindowError
from rasterio.io import DatasetReader
from rasterio.warp import reproject, transform_bounds
from rasterio.windows import Window, from_bounds

from osm3d.acquisition.inventory import PARTIAL_SUFFIX, SUFFIXES

log = logging.getLogger(__name__)

TILE_PX = 256
MERCATOR_HALF = 20037508.342789244
_YEAR_RE = re.compile(r"ORTOS-(\d{4})")


def tile_bounds_3857(z: int, x: int, y: int) -> tuple[float, float, float, float]:
    size = 2 * MERCATOR_HALF / 2**z
    x0 = -MERCATOR_HALF + x * size
    y1 = MERCATOR_HALF - y * size
    return (x0, y1 - size, x0 + size, y1)


@dataclass(frozen=True)
class OrthoFile:
    path: Path
    year: int
    bounds_3857: tuple[float, float, float, float]
    bounds_wgs84: tuple[float, float, float, float]


class OrthoTiles:
    def __init__(self, directory: Path, cache_tiles: int = 400) -> None:
        self.directory = directory
        self._lock = threading.Lock()
        self._files: list[OrthoFile] = []
        self._key: tuple[tuple[str, float, int], ...] | None = None
        self._tiles: OrderedDict[tuple[int, int, int], bytes | None] = OrderedDict()
        self._cache_tiles = cache_tiles

    # ------------------------------------------------------------- ficheiros

    def files(self) -> list[OrthoFile]:
        """Ortofotos completas na pasta (revistas se a pasta mudar)."""
        import rasterio

        paths = (
            sorted(
                p
                for p in self.directory.iterdir()
                if p.is_file()
                and p.suffix.lower() in SUFFIXES["orthophoto"]
                and not p.name.endswith(PARTIAL_SUFFIX)
            )
            if self.directory.is_dir()
            else []
        )
        key = tuple((str(p), p.stat().st_mtime, p.stat().st_size) for p in paths)
        with self._lock:
            if key == self._key:
                return self._files
            found = []
            for p in paths:
                try:
                    with rasterio.open(p) as src:
                        b3857 = transform_bounds(src.crs, "EPSG:3857", *src.bounds)
                        b4326 = transform_bounds(src.crs, "EPSG:4326", *src.bounds)
                except Exception as exc:  # ficheiro corrompido: ignorar, não parar o mapa
                    log.warning("Ortofoto ignorada (%s): %s", p.name, exc)
                    continue
                m = _YEAR_RE.search(p.name)
                found.append(OrthoFile(p, int(m.group(1)) if m else 0, b3857, b4326))
            # Mais antigas primeiro: as recentes são pintadas por cima.
            self._files = sorted(found, key=lambda f: (f.year, f.path.name))
            self._key = key
            self._tiles.clear()
            return self._files

    # ----------------------------------------------------------------- tiles

    def tile(self, z: int, x: int, y: int) -> bytes | None:
        """PNG RGBA do tile, ou ``None`` se nenhuma ortofoto o interseta."""
        if not (0 <= z <= 24 and 0 <= x < 2**z and 0 <= y < 2**z):
            raise ValueError("Tile inválido")
        files = self.files()
        key = (z, x, y)
        with self._lock:
            if key in self._tiles:
                self._tiles.move_to_end(key)
                return self._tiles[key]
        tb = tile_bounds_3857(z, x, y)
        hits = [
            f
            for f in files
            if f.bounds_3857[0] < tb[2]
            and tb[0] < f.bounds_3857[2]
            and f.bounds_3857[1] < tb[3]
            and tb[1] < f.bounds_3857[3]
        ]
        out = self._render(hits, tb) if hits else None
        with self._lock:
            self._tiles[key] = out
            while len(self._tiles) > self._cache_tiles:
                self._tiles.popitem(last=False)
        return out

    def _render(
        self, files: list[OrthoFile], tb: tuple[float, float, float, float]
    ) -> bytes | None:
        import rasterio

        rgba = np.zeros((4, TILE_PX, TILE_PX), np.uint8)
        dst_transform = Affine(
            (tb[2] - tb[0]) / TILE_PX, 0, tb[0], 0, -(tb[3] - tb[1]) / TILE_PX, tb[3]
        )
        for f in files:
            with rasterio.open(f.path) as src:
                part = _read_reprojected(src, tb, dst_transform)
            if part is None:
                continue
            rgb, alpha = part
            on = alpha > 0
            rgba[:3, on] = rgb[:, on]
            rgba[3, on] = 255
        if not rgba[3].any():
            return None
        buf = io.BytesIO()
        Image.fromarray(np.moveaxis(rgba, 0, -1), "RGBA").save(buf, "PNG", optimize=False)
        return buf.getvalue()


def _bands(src: DatasetReader) -> tuple[list[int], int | None]:
    """Bandas RGB (1-based) e banda alfa, pela interpretação de cor do ficheiro."""
    interp = list(src.colorinterp)
    rgb = [
        interp.index(c) + 1
        for c in (ColorInterp.red, ColorInterp.green, ColorInterp.blue)
        if c in interp
    ]
    if len(rgb) != 3:
        rgb = [1, 1, 1] if src.count < 3 else [1, 2, 3]
    alpha = interp.index(ColorInterp.alpha) + 1 if ColorInterp.alpha in interp else None
    return rgb, alpha


def _read_reprojected(
    src: DatasetReader, tb: tuple[float, float, float, float], dst_transform: Affine
) -> tuple[np.ndarray, np.ndarray] | None:
    """RGB e alfa de ``src`` reprojetados para o tile (lê ao nível de overview certo)."""
    sb = transform_bounds("EPSG:3857", src.crs, *tb, densify_pts=21)
    full = Window(0, 0, src.width, src.height)
    try:
        win = from_bounds(*sb, transform=src.transform).intersection(full)
    except WindowError:  # sem interseção
        return None
    win = win.round_offsets().round_lengths()
    if win.width < 1 or win.height < 1:
        return None
    # Ler a ~1,5× a resolução do tile: com out_shape menor, o GDAL usa a overview certa.
    scale = max(1.0, (sb[2] - sb[0]) / TILE_PX / 1.5 / abs(src.transform.a))
    ow, oh = max(1, math.ceil(win.width / scale)), max(1, math.ceil(win.height / scale))
    rgb_bands, alpha_band = _bands(src)
    data = src.read(rgb_bands, window=win, out_shape=(3, oh, ow), resampling=Resampling.bilinear)
    alpha = (
        src.read(alpha_band, window=win, out_shape=(oh, ow))
        if alpha_band
        else src.dataset_mask(window=win, out_shape=(oh, ow))
    )
    common = {
        "src_transform": src.window_transform(win) * Affine.scale(win.width / ow, win.height / oh),
        "src_crs": src.crs,
        "dst_transform": dst_transform,
        "dst_crs": "EPSG:3857",
        "src_nodata": None,
        "dst_nodata": 0,
    }
    rgb = np.zeros((3, TILE_PX, TILE_PX), np.uint8)
    a = np.zeros((TILE_PX, TILE_PX), np.uint8)
    reproject(data, rgb, resampling=Resampling.bilinear, **common)
    reproject(alpha, a, resampling=Resampling.nearest, **common)
    return rgb, a
