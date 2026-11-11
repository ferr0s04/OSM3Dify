"""Obtenção de ortofotos (Fase 5 — cores de telhado).

Atenção: ortofotos convencionais não são "true ortho"; o telhado aparece deslocado
do footprint. A amostragem de cor usa a máscara de telhado derivada do LiDAR.
"""

from __future__ import annotations

from pathlib import Path


def fetch_orthophotos(bbox_3763: tuple[float, float, float, float], out_dir: Path) -> list[Path]:
    raise NotImplementedError("Fase 5: ortofotos")
