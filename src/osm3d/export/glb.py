"""Exportação GLB para inspeção visual (requer o extra ``mesh``: ``uv sync --extra mesh``).

Não é o produto final: o renderizador de referência é o OSM2World a partir do ``.osm``
gerado, porque interpreta as tags S3DB como os mapas 3D reais.
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

from osm3d.domain.models import Building


def write_glb(buildings: Iterable[Building], path: Path) -> None:
    raise NotImplementedError("Fase 4: exportação GLB com trimesh")
