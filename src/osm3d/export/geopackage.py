"""Persistência do modelo intermédio em GeoPackage.

Camadas: ``buildings``, ``building_parts``, ``qa_flags``. Abre diretamente no QGIS
e serve de artefacto entre fases do pipeline.
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

from osm3d.domain.models import Building


def write_buildings(buildings: Iterable[Building], path: Path, crs: str) -> None:
    raise NotImplementedError("Fase 2: escrita do modelo intermédio em GeoPackage")


def read_buildings(path: Path) -> list[Building]:
    raise NotImplementedError("Fase 2: leitura do modelo intermédio de GeoPackage")
