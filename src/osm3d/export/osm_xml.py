"""Exportação para ficheiro ``.osm`` no formato JOSM, para revisão humana.

Regras:
- Os elementos modificados levam ``action="modify"`` e mantêm ``id`` e ``version``
  originais; o upload é feito por uma pessoa, no JOSM. Este módulo nunca publica.
- As tags são juntas com :func:`osm3d.export.s3db_tags.merge_tags` — conflitos
  vão para o relatório, não para o ficheiro.
- Exportação de ``building:part`` (Fase 6) exige partilhar nós com o contorno.
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

from osm3d.domain.models import Building


def write_josm_file(buildings: Iterable[Building], path: Path) -> None:
    raise NotImplementedError("Fase 3b: exportação OSM XML para JOSM")
