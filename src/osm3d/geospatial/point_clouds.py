"""Leitura de LAZ (laspy) e recorte de pontos por footprint.

PDAL é opcional e externo (pipelines JSON via CLI) para pré-processamento pesado.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np
import numpy.typing as npt


@dataclass(frozen=True)
class PointSet:
    xyz: npt.NDArray[np.float64]  # (N, 3), EPSG:3763
    classification: npt.NDArray[np.uint8]  # (N,), classes ASPRS
    rgb: npt.NDArray[np.uint16] | None = None

    def __len__(self) -> int:
        return int(self.xyz.shape[0])

    def with_classes(self, classes: list[int]) -> PointSet:
        mask = np.isin(self.classification, classes)
        rgb = self.rgb[mask] if self.rgb is not None else None
        return PointSet(self.xyz[mask], self.classification[mask], rgb)


def read_points(laz_files: list[Path], bbox_3763: tuple[float, float, float, float]) -> PointSet:
    raise NotImplementedError("Fase 1: leitura LAZ")
