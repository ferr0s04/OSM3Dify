"""Segmentação de edifícios em volumes (Fase 4).

Não basta separar por altura: um telhado com várias águas não é vários volumes.
Combina descontinuidades no nDSM, coerência do modelo de telhado por região e o
footprint; as fronteiras são regularizadas para polígonos retilíneos alinhados com
o eixo do edifício (o S3DB renderiza mal telhados inclinados em partes irregulares).
"""

from __future__ import annotations

from osm3d.domain.models import Building


def split_into_parts(building: Building) -> Building:
    raise NotImplementedError("Fase 4: segmentação em partes")
