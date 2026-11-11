"""MDT/MDS (rasterio): amostragem da cota do terreno à volta de cada edifício."""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path
from types import TracebackType

import numpy as np
import rasterio
from rasterio.features import geometry_mask
from rasterio.windows import from_bounds
from shapely.geometry.base import BaseGeometry


class DtmMosaic:
    """Conjunto de quadrículas MDT, amostradas como se fossem um só raster."""

    def __init__(self, paths: Iterable[Path]) -> None:
        self._datasets = [rasterio.open(p) for p in paths]

    def __enter__(self) -> DtmMosaic:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    def close(self) -> None:
        for ds in self._datasets:
            ds.close()

    def values_in(self, geometry: BaseGeometry) -> np.ndarray:
        """Cotas válidas das células cujo centro está dentro de ``geometry``."""
        out: list[np.ndarray] = []
        minx, miny, maxx, maxy = geometry.bounds
        for ds in self._datasets:
            b = ds.bounds
            if maxx < b.left or minx > b.right or maxy < b.bottom or miny > b.top:
                continue
            win = (
                from_bounds(
                    max(minx, b.left),
                    max(miny, b.bottom),
                    min(maxx, b.right),
                    min(maxy, b.top),
                    ds.transform,
                )
                .round_offsets()
                .round_lengths()
            )
            if win.width < 1 or win.height < 1:
                continue
            data = ds.read(1, window=win, masked=True)
            inside = geometry_mask(
                [geometry], out_shape=data.shape, transform=ds.window_transform(win), invert=True
            )
            vals = data[inside & ~np.ma.getmaskarray(data)]
            out.append(np.asarray(vals, dtype=np.float64))
        return np.concatenate(out) if out else np.empty(0)


def ground_ring(footprint: BaseGeometry, buffer_m: float, inner_gap_m: float = 0.5) -> BaseGeometry:
    """Anel à volta do footprint (exclui uma faixa junto às paredes)."""
    return footprint.buffer(buffer_m).difference(footprint.buffer(inner_gap_m))


def sample_ground_elevation(
    dtm: DtmMosaic, footprint: BaseGeometry, buffer_m: float, percentile: float
) -> float | None:
    """Cota do terreno: percentil baixo do MDT num anel à volta do footprint.

    S3DB mede ``height`` a partir do ponto mais baixo em contacto com o terreno;
    um percentil baixo (não o mínimo) segue essa convenção sendo robusto a ruído.
    """
    vals = dtm.values_in(ground_ring(footprint, buffer_m))
    if vals.size < 4:
        vals = dtm.values_in(footprint.buffer(buffer_m))
    return float(np.percentile(vals, percentile)) if vals.size else None
