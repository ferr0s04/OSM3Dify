"""Leitura de LAZ (laspy, por blocos) e atribuição de pontos a footprints.

PDAL é opcional e externo (pipelines JSON via CLI) para pré-processamento pesado.
"""

from __future__ import annotations

import logging
import os
import re
import threading
from collections import OrderedDict, defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import laspy
import numpy as np
import numpy.typing as npt
import shapely
from shapely.geometry.base import BaseGeometry

log = logging.getLogger(__name__)

_TILE_RE = re.compile(r"-(\d{6})-")


@dataclass(frozen=True)
class PointSet:
    xyz: npt.NDArray[np.float64]  # (N, 3), EPSG:3763
    classification: npt.NDArray[np.uint8]  # (N,), classes ASPRS
    rgb: npt.NDArray[np.uint16] | None = None  # (N, 3), 16 bits
    nir: npt.NDArray[np.uint16] | None = None  # (N,)

    def __len__(self) -> int:
        return int(self.xyz.shape[0])

    def subset(self, mask: npt.NDArray[np.bool_] | npt.NDArray[np.intp]) -> PointSet:
        return PointSet(
            self.xyz[mask],
            self.classification[mask],
            self.rgb[mask] if self.rgb is not None else None,
            self.nir[mask] if self.nir is not None else None,
        )

    def with_classes(self, classes: Sequence[int]) -> PointSet:
        return self.subset(np.isin(self.classification, classes))

    @staticmethod
    def empty() -> PointSet:
        return PointSet(np.empty((0, 3)), np.empty(0, dtype=np.uint8))

    @staticmethod
    def concat(parts: Iterable[PointSet]) -> PointSet:
        parts = [p for p in parts if len(p)]
        if not parts:
            return PointSet.empty()
        has_rgb = all(p.rgb is not None for p in parts)
        has_nir = all(p.nir is not None for p in parts)
        return PointSet(
            np.concatenate([p.xyz for p in parts]),
            np.concatenate([p.classification for p in parts]),
            np.concatenate([p.rgb for p in parts]) if has_rgb else None,
            np.concatenate([p.nir for p in parts]) if has_nir else None,
        )


def index_tiles(directory: Path, suffixes: tuple[str, ...]) -> dict[str, Path]:
    """Ficheiros DGT por identificador de quadrícula (fica a versão mais recente)."""
    found: dict[str, Path] = {}
    if not directory.exists():
        return found
    for path in sorted(directory.iterdir()):  # _v05 ordena depois de _v03
        if path.suffix.lower() in suffixes and (m := _TILE_RE.search(path.name)):
            found[m.group(1)] = path
    return found


# ------------------------------------------------- pontos descomprimidos em cache
#
# As fases leem as mesmas quadrículas várias vezes (deteção dos candidatos, telhados,
# cor; e cada caixa com margem apanha também a quadrícula vizinha), e descomprimir um LAZ
# leva ~3 s. Os pontos de um conjunto de classes (p.ex. só edifícios) de cada LAZ ficam:
#
# - em disco (``cache_dir``), com as coordenadas inteiras do LAS: o resultado é igual,
#   bit a bit, ao da leitura do LAZ; invalida-se se o LAZ mudar (tamanho, data);
# - em memória, nos últimos LAZ lidos neste processo.

POINT_CACHE_VERSION = 1
_MEMO_MAX = 4
_memo: OrderedDict[tuple[str, tuple[int, ...]], tuple[tuple[int, int], PointSet]] = OrderedDict()
_memo_lock = threading.Lock()


def _stamp(path: Path) -> tuple[int, int]:
    st = path.stat()
    return st.st_size, st.st_mtime_ns


def point_cache_file(cache_dir: Path, laz: Path, classes: Sequence[int]) -> Path:
    return cache_dir / f"{laz.stem}.c{'-'.join(str(c) for c in sorted(classes))}.npz"


def _decode(path: Path, classes: tuple[int, ...]) -> dict[str, npt.NDArray[Any]]:
    """Os pontos destas classes de um LAZ inteiro, com as coordenadas inteiras do LAS."""
    cols: dict[str, list[npt.NDArray[Any]]] = defaultdict(list)
    with laspy.open(path) as reader:
        h = reader.header
        dims = set(h.point_format.dimension_names)
        for pts in reader.chunk_iterator(5_000_000):
            cls = np.asarray(pts.classification)
            m = np.isin(cls, classes)
            if not m.any():
                continue
            cols["ixyz"].append(
                np.column_stack([np.asarray(pts.X)[m], np.asarray(pts.Y)[m], np.asarray(pts.Z)[m]])
            )
            cols["cls"].append(cls[m].astype(np.uint8))
            if {"red", "green", "blue"} <= dims:
                cols["rgb"].append(
                    np.column_stack(
                        [np.asarray(pts.red)[m], np.asarray(pts.green)[m], np.asarray(pts.blue)[m]]
                    ).astype(np.uint16)
                )
            if "nir" in dims:
                cols["nir"].append(np.asarray(pts.nir)[m].astype(np.uint16))
        out: dict[str, npt.NDArray[Any]] = {
            "scales": np.asarray(h.scales, dtype=np.float64),
            "offsets": np.asarray(h.offsets, dtype=np.float64),
            "ixyz": (np.concatenate(cols["ixyz"]) if cols["ixyz"] else np.empty((0, 3), np.int32)),
            "cls": np.concatenate(cols["cls"]) if cols["cls"] else np.empty(0, np.uint8),
        }
    for k in ("rgb", "nir"):
        if cols[k]:
            out[k] = np.concatenate(cols[k])
    return out


def _to_pointset(d: dict[str, npt.NDArray[Any]]) -> PointSet:
    # Como o laspy: inteiro × escala + offset, em float64 (o mesmo valor que ler o LAZ).
    xyz = d["ixyz"].astype(np.float64) * d["scales"] + d["offsets"]
    return PointSet(xyz, d["cls"], d.get("rgb"), d.get("nir"))


def _cached_tile(path: Path, classes: tuple[int, ...], cache_dir: Path) -> PointSet:
    """Os pontos destas classes de um LAZ (da memória, do disco ou do LAZ)."""
    key = (str(path.resolve()), classes)
    stamp = _stamp(path)
    with _memo_lock:
        hit = _memo.get(key)
        if hit is not None and hit[0] == stamp:
            _memo.move_to_end(key)
            return hit[1]
    cache = point_cache_file(cache_dir, path, classes)
    ps = None
    if cache.exists():
        try:
            with np.load(cache) as d:
                if int(d["version"]) == POINT_CACHE_VERSION and tuple(d["stamp"]) == stamp:
                    ps = _to_pointset({k: d[k] for k in d.files})
        except (OSError, ValueError, KeyError) as exc:
            log.warning("Cache de pontos ilegível (%s): a ler o LAZ", exc)
    if ps is None:
        data = _decode(path, classes)
        cache_dir.mkdir(parents=True, exist_ok=True)
        # Escreve-se à parte e troca-se no fim: nunca fica um ficheiro a meio, e vários
        # processos (deteção em paralelo) podem estar a criar a mesma cache.
        tmp = cache.with_name(f"{cache.stem}.{os.getpid()}.tmp.npz")
        try:
            np.savez(tmp, version=np.array(POINT_CACHE_VERSION), stamp=np.array(stamp), **data)
            tmp.replace(cache)
        except OSError as exc:  # p.ex. outro processo tem-na aberta (Windows)
            log.debug("Cache de pontos não gravada (%s)", exc)
            tmp.unlink(missing_ok=True)
        ps = _to_pointset(data)
    with _memo_lock:
        _memo[key] = (stamp, ps)
        _memo.move_to_end(key)
        while len(_memo) > _MEMO_MAX:
            _memo.popitem(last=False)
    return ps


def _laz_bounds(path: Path) -> tuple[float, float, float, float]:
    with laspy.open(path) as reader:
        h = reader.header
        return float(h.mins[0]), float(h.mins[1]), float(h.maxs[0]), float(h.maxs[1])


def read_points(
    laz_files: Iterable[Path],
    bbox: tuple[float, float, float, float],
    classes: Sequence[int] | None = None,
    chunk_size: int = 5_000_000,
    cache_dir: Path | None = None,
) -> PointSet:
    """Lê pontos dentro de ``bbox`` (EPSG:3763), por blocos, só com os campos necessários.

    ``cache_dir`` (com ``classes``): os pontos dessas classes de cada LAZ ficam em cache
    (disco e memória), para as leituras seguintes da mesma quadrícula.
    """
    minx, miny, maxx, maxy = bbox
    parts: list[PointSet] = []
    for path in laz_files:
        if cache_dir is not None and classes is not None:
            x0, y0, x1, y1 = _laz_bounds(path)
            if x1 < minx or x0 > maxx or y1 < miny or y0 > maxy:
                continue
            ps = _cached_tile(path, tuple(sorted(set(classes))), cache_dir)
            x, y = ps.xyz[:, 0], ps.xyz[:, 1]
            parts.append(ps.subset((x >= minx) & (x <= maxx) & (y >= miny) & (y <= maxy)))
            continue
        with laspy.open(path) as reader:
            h = reader.header
            if h.maxs[0] < minx or h.mins[0] > maxx or h.maxs[1] < miny or h.mins[1] > maxy:
                continue
            dims = set(h.point_format.dimension_names)
            for pts in reader.chunk_iterator(chunk_size):
                x, y = np.asarray(pts.x), np.asarray(pts.y)
                m = (x >= minx) & (x <= maxx) & (y >= miny) & (y <= maxy)
                cls = np.asarray(pts.classification)
                if classes is not None:
                    m &= np.isin(cls, classes)
                if not m.any():
                    continue
                rgb = (
                    np.column_stack(
                        [np.asarray(pts.red)[m], np.asarray(pts.green)[m], np.asarray(pts.blue)[m]]
                    )
                    if {"red", "green", "blue"} <= dims
                    else None
                )
                nir = np.asarray(pts.nir)[m] if "nir" in dims else None
                parts.append(
                    PointSet(
                        np.column_stack([x[m], y[m], np.asarray(pts.z)[m]]),
                        cls[m].astype(np.uint8),
                        rgb,
                        nir,
                    )
                )
    points = PointSet.concat(parts)
    log.info("%d pontos lidos", len(points))
    return points


def assign_points(
    points: PointSet,
    footprints: Sequence[BaseGeometry],
    offset: tuple[float, float] = (0.0, 0.0),
) -> list[npt.NDArray[np.intp]]:
    """Índices dos pontos dentro de cada footprint (deslocado por ``offset``)."""
    dx, dy = offset
    x, y = points.xyz[:, 0], points.xyz[:, 1]
    # Pontos ordenados por x: os candidatos de cada footprint são uma fatia (a caixa dele),
    # sem uma árvore com todos os pontos.
    by_x = np.argsort(x, kind="stable")
    xs = x[by_x]
    out: list[npt.NDArray[np.intp]] = []
    for f in footprints:
        g = shapely.transform(f, lambda c: c + np.array([dx, dy]))
        x0, y0, x1, y1 = g.bounds
        idx = by_x[np.searchsorted(xs, x0, side="left") : np.searchsorted(xs, x1, side="right")]
        idx = idx[(y[idx] >= y0) & (y[idx] <= y1)]
        shapely.prepare(g)
        # Estritamente dentro (como "within"); por ordem crescente, como antes.
        out.append(np.sort(idx[shapely.contains_xy(g, x[idx], y[idx])]).astype(np.intp))
    return out
