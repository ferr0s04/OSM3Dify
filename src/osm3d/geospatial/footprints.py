"""Leitura, reprojeção (WGS84 → EPSG:3763) e limpeza de footprints OSM."""

from __future__ import annotations

from pathlib import Path

from osm3d.domain.models import Building


def load_footprints(osm_file: Path, crs: str, min_area_m2: float) -> list[Building]:
    """Constrói ``Building`` (sem partes) a partir do ``.osm`` descarregado.

    Corrige geometrias inválidas (``shapely.make_valid``), descarta áreas
    inferiores a ``min_area_m2`` e preserva ``id``/``version``/tags originais.
    """
    raise NotImplementedError("Fase 1: footprints")
