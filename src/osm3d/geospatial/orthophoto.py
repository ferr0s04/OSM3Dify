"""Ortofotos (R, G, B, NIR) da DGT lidas por janelas, na grelha de 25 cm."""

from __future__ import annotations

import math
from pathlib import Path

import numpy as np
import rasterio
from rasterio.windows import from_bounds

RES_M = 0.25  # resolução do ORTOS


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
