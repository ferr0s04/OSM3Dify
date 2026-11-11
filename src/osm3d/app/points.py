"""Cache de pontos LiDAR por quadrícula, para servir a vista 3D sem reler o LAZ.

Na primeira utilização de cada quadrícula lê-se o LAZ por blocos e guarda-se um
``.npz`` com todos os pontos de edifício (classe 6) e uma amostra (1/8) de solo e
vegetação, que dão contexto. As leituras seguintes vêm do ``.npz`` (rápido).
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path

import laspy
import numpy as np

from osm3d.geospatial.point_clouds import PointSet
from osm3d.geospatial.tiling import TileKey, tile_of

log = logging.getLogger(__name__)

BUILDING_CLASS = 6
CONTEXT_CLASSES = (2, 3, 4, 5)
CONTEXT_DECIMATION = 8


def _read_tile(path: Path, seed: int = 0) -> PointSet:
    rng = np.random.default_rng(seed)
    parts: list[PointSet] = []
    with laspy.open(path) as reader:
        for pts in reader.chunk_iterator(5_000_000):
            cls = np.asarray(pts.classification)
            keep = (cls == BUILDING_CLASS) | (
                np.isin(cls, CONTEXT_CLASSES) & (rng.random(len(cls)) < 1 / CONTEXT_DECIMATION)
            )
            rgb = np.column_stack(
                [np.asarray(pts.red)[keep], np.asarray(pts.green)[keep], np.asarray(pts.blue)[keep]]
            )
            parts.append(
                PointSet(
                    np.column_stack(
                        [np.asarray(pts.x)[keep], np.asarray(pts.y)[keep], np.asarray(pts.z)[keep]]
                    ),
                    cls[keep].astype(np.uint8),
                    (rgb >> 8).astype(np.uint16),  # 16 → 8 bits
                )
            )
    return PointSet.concat(parts)


CACHE_VERSION = 2


def _save(path: Path, ps: PointSet) -> None:
    """Formato compacto: float32 relativo à origem, RGB 8 bits, comprimido (~4× menor)."""
    origin = ps.xyz.min(axis=0) if len(ps) else np.zeros(3)
    rgb = ps.rgb if ps.rgb is not None else np.zeros((len(ps), 3), np.uint16)
    np.savez_compressed(
        path,
        version=np.array(CACHE_VERSION),
        origin=origin,
        xyz=(ps.xyz - origin).astype(np.float32),
        cls=ps.classification,
        rgb=np.clip(rgb, 0, 255).astype(np.uint8),
    )


def _load(path: Path) -> PointSet | None:
    d = np.load(path)
    if "version" not in d or int(d["version"]) != CACHE_VERSION:
        return None  # formato antigo: refazer
    xyz = d["xyz"].astype(np.float64) + d["origin"]
    return PointSet(xyz, d["cls"], d["rgb"].astype(np.uint16))


class TilePointCache:
    def __init__(
        self, lidar_tiles: dict[str, Path], cache_dir: Path, max_in_memory: int = 6
    ) -> None:
        self.lidar_tiles = lidar_tiles
        self.cache_dir = cache_dir
        self.max_in_memory = max_in_memory
        self._mem: dict[str, PointSet] = {}  # ordem de inserção = ordem de uso (LRU)
        self._lock = threading.Lock()

    def _tile(self, name: str) -> PointSet | None:
        src = self.lidar_tiles.get(name)
        if src is None:
            return None
        with self._lock:
            if name in self._mem:
                self._mem[name] = self._mem.pop(name)  # marca como usado recentemente
                return self._mem[name]
            cache = self.cache_dir / f"{src.stem}.npz"
            ps = _load(cache) if cache.exists() else None
            if ps is None:
                log.info("A preparar cache de pontos de %s (só na primeira vez)…", src.name)
                ps = _read_tile(src)
                self.cache_dir.mkdir(parents=True, exist_ok=True)
                _save(cache, ps)
            self._mem[name] = ps
            while len(self._mem) > self.max_in_memory:
                self._mem.pop(next(iter(self._mem)))
            return ps

    def around(self, bounds: tuple[float, float, float, float]) -> PointSet:
        minx, miny, maxx, maxy = bounds
        lo, hi = tile_of(minx, miny), tile_of(maxx, maxy)
        parts = []
        for ix in range(lo.ix, hi.ix + 1):
            for iy in range(lo.iy, hi.iy + 1):
                ps = self._tile(TileKey(ix, iy).dgt_name)
                if ps is None:
                    continue
                x, y = ps.xyz[:, 0], ps.xyz[:, 1]
                parts.append(ps.subset((x >= minx) & (x <= maxx) & (y >= miny) & (y <= maxy)))
        return PointSet.concat(parts)
