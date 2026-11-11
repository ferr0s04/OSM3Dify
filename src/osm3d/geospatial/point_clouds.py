"""Leitura de LAZ (laspy, por blocos) e atribuição de pontos a footprints.

PDAL é opcional e externo (pipelines JSON via CLI) para pré-processamento pesado.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

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


def read_points(
    laz_files: Iterable[Path],
    bbox: tuple[float, float, float, float],
    classes: Sequence[int] | None = None,
    chunk_size: int = 5_000_000,
) -> PointSet:
    """Lê pontos dentro de ``bbox`` (EPSG:3763), por blocos, só com os campos necessários."""
    minx, miny, maxx, maxy = bbox
    parts: list[PointSet] = []
    for path in laz_files:
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
    shifted = [shapely.transform(f, lambda c: c + np.array([dx, dy])) for f in footprints]
    tree = shapely.STRtree(shifted)
    pts = shapely.points(points.xyz[:, 0], points.xyz[:, 1])
    pt_idx, fp_idx = tree.query(pts, predicate="within")
    order = np.argsort(fp_idx, kind="stable")
    pt_idx, fp_idx = pt_idx[order], fp_idx[order]
    bounds = np.searchsorted(fp_idx, np.arange(len(footprints) + 1))
    return [pt_idx[bounds[i] : bounds[i + 1]] for i in range(len(footprints))]
