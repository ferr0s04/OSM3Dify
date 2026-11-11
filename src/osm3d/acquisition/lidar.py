"""Obtenção de nuvens LiDAR e MDT da DGT.

LiDAR DGT: LAZ (LAS 1.4) classificado, ≥10 pts/m², quadrículas 1 km × 1 km em
EPSG:3763, licença CC BY 4.0. Download no Centro de Dados da DGT (conta + STAC).
Ver docs/architecture/revisao-arquitetura.md §8 sobre compatibilidade com a ODbL.
"""

from __future__ import annotations

from pathlib import Path


def tiles_for_bbox(bbox_3763: tuple[float, float, float, float]) -> list[str]:
    """Identificadores das quadrículas de 1 km que intersetam a bbox (EPSG:3763)."""
    raise NotImplementedError("Fase 1: índice de quadrículas DGT")


def download_tiles(tile_ids: list[str], stac_url: str, out_dir: Path) -> list[Path]:
    raise NotImplementedError("Fase 1: download STAC DGT")
