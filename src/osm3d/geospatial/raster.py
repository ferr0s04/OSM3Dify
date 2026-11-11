"""MDT/MDS e nDSM (rasterio).

O nDSM (MDS − MDT, 0.25–0.5 m) é a via principal para alturas e segmentação
em partes; os pontos ficam para o ajuste fino de telhados.
"""

from __future__ import annotations

from pathlib import Path


def build_ndsm(points_laz: list[Path], dtm: Path, resolution_m: float, out: Path) -> Path:
    raise NotImplementedError("Fase 2: nDSM")


def sample_ground_elevation(dtm: Path, ring_geometry: object, percentile: float) -> float:
    """Cota do terreno: percentil baixo do MDT num anel à volta do footprint."""
    raise NotImplementedError("Fase 2: cota do terreno")
