"""Relação entre o que a análise propõe e o que já está mapeado no OSM.

Todos os edifícios vêm do OSM. O que interessa distinguir é se **já têm informação 3D**
mapeada por pessoas e, nesse caso, se a análise a confirma, a complementa ou diverge
dela. Os valores OSM nunca são sobrescritos por omissão (ver ``merge_tags``); os
conflitos ficam destacados para revisão.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from osm3d.export.s3db_tags import TagMerge

# Tags que descrevem a forma/aparência 3D de um edifício (Simple 3D Buildings).
EXISTING_3D_TAGS = (
    "height",
    "min_height",
    "building:levels",
    "building:min_level",
    "roof:shape",
    "roof:height",
    "roof:levels",
    "roof:orientation",
    "roof:direction",
    "roof:colour",
    "roof:material",
    "building:colour",
    "building:material",
)


class ChangeKind(StrEnum):
    NEW = "novo"  # sem 3D no OSM: tudo o que se propõe é novo
    COMPLEMENTS = "complementa"  # já tem 3D; acrescentam-se tags em falta, sem contradizer
    DIVERGES = "diverge"  # a análise propõe valores diferentes dos do OSM
    SAME = "igual"  # já tem 3D e a análise não acrescenta nada
    NO_PROPOSAL = "sem proposta"  # não foi possível reconstruir


@dataclass
class ChangeSummary:
    kind: ChangeKind
    existing_3d: dict[str, str] = field(default_factory=dict)
    osm_parts: int = 0
    added: dict[str, str] = field(default_factory=dict)
    conflicts: dict[str, tuple[str, str]] = field(default_factory=dict)

    @property
    def has_osm_3d(self) -> bool:
        return bool(self.existing_3d) or self.osm_parts > 0


def existing_3d(tags: dict[str, str]) -> dict[str, str]:
    return {k: tags[k] for k in EXISTING_3D_TAGS if k in tags}


def classify_change(
    existing_tags: dict[str, str], merge: TagMerge | None, osm_parts: int = 0
) -> ChangeSummary:
    ex = existing_3d(existing_tags)
    has_3d = bool(ex) or osm_parts > 0
    if merge is None:
        return ChangeSummary(ChangeKind.NO_PROPOSAL, ex, osm_parts)
    if merge.conflicts:
        kind = ChangeKind.DIVERGES
    elif merge.added or merge.changed:
        kind = ChangeKind.COMPLEMENTS if has_3d else ChangeKind.NEW
    else:
        kind = ChangeKind.SAME if has_3d else ChangeKind.NEW
    return ChangeSummary(kind, ex, osm_parts, dict(merge.added), dict(merge.conflicts))
