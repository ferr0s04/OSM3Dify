"""Exportação para ficheiro ``.osm`` no formato JOSM, para revisão humana.

Regras:

- Os elementos modificados levam ``action="modify"`` e mantêm ``id`` e ``version``
  originais. Os nós e as ways-membro vão sem alterações, só para o JOSM conseguir
  desenhar e validar a geometria. Este módulo nunca publica.
- As tags são juntas com :func:`osm3d.export.s3db_tags.merge_tags`: tags mapeadas por
  humanos não são sobrescritas (salvo opção explícita) e os conflitos vão para o
  relatório.
- Upload permitido por omissão (sem atributo ``upload``). O upload é sempre feito
  por uma pessoa, no JOSM. ``josm_upload: never`` no config volta a bloqueá-lo.
- ``building:part`` (Fase 6) ainda não é exportado: só edifícios de uma parte.
"""

from __future__ import annotations

import copy
import csv
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal
from xml.etree import ElementTree as ET

from osm3d.domain.models import Building
from osm3d.export.s3db_tags import TagPolicy, merge_tags, part_to_tags

UploadPolicy = Literal["never", "false", "true"]


@dataclass
class ExportRecord:
    key: str
    exported: bool
    reason: str
    added: dict[str, str] = field(default_factory=dict)
    changed: dict[str, tuple[str, str]] = field(default_factory=dict)
    conflicts: dict[str, tuple[str, str]] = field(default_factory=dict)


def plan_export(
    buildings: Iterable[Building],
    policy: TagPolicy,
    *,
    include_pending: bool = False,
    overwrite: bool = False,
) -> tuple[list[tuple[Building, dict[str, str]]], list[ExportRecord]]:
    """Decide o que exportar. Devolve ((edifício, tags finais), registos)."""
    selected: list[tuple[Building, dict[str, str]]] = []
    records: list[ExportRecord] = []
    for b in buildings:
        accepted = b.review_status == "accepted"
        reason = None
        if b.review_status == "rejected":
            reason = "rejeitado na revisão"
        elif not accepted and not include_pending:
            reason = "por rever"
        elif not accepted and b.needs_review:
            reason = "sinalizado: " + ",".join(f.code for f in b.qa_flags if f.severity != "info")
        elif len(b.parts) != 1:
            reason = "sem partes" if not b.parts else "várias partes (Fase 6)"
        if reason:
            records.append(ExportRecord(b.key, False, reason))
            continue
        merge = merge_tags(b.existing_tags, part_to_tags(b.parts[0], policy), overwrite=overwrite)
        record = ExportRecord(
            b.key,
            merge.has_changes,
            "ok" if merge.has_changes else "sem alterações",
            merge.added,
            merge.changed,
            merge.conflicts,
        )
        records.append(record)
        if merge.has_changes:
            selected.append((b, merge.tags))
    return selected, records


def _index(root: ET.Element) -> dict[tuple[str, int], ET.Element]:
    return {
        (el.tag, int(el.attrib["id"])): el for el in root if el.tag in ("node", "way", "relation")
    }


def _set_tags(el: ET.Element, tags: dict[str, str]) -> None:
    for t in el.findall("tag"):
        el.remove(t)
    for k in sorted(tags):
        ET.SubElement(el, "tag", k=k, v=tags[k])


def write_josm_file(
    selected: list[tuple[Building, dict[str, str]]],
    source_osm: Path,
    out: Path,
    *,
    upload: UploadPolicy = "true",
) -> Path:
    """Escreve os edifícios selecionados (com as tags finais) num ``.osm`` para o JOSM."""
    index = _index(ET.parse(source_osm).getroot())
    nodes: dict[int, ET.Element] = {}
    ways: dict[int, ET.Element] = {}
    relations: dict[int, ET.Element] = {}

    def add_way(way_id: int, tags: dict[str, str] | None = None) -> None:
        src = index.get(("way", way_id))
        if src is None:
            raise KeyError(f"way {way_id} não está em {source_osm}")
        el = copy.deepcopy(src)
        if tags is not None:
            _set_tags(el, tags)
            el.set("action", "modify")
        ways[way_id] = el
        for nd in src.findall("nd"):
            ref = int(nd.attrib["ref"])
            if ref not in nodes:
                nodes[ref] = copy.deepcopy(index[("node", ref)])

    for b, tags in selected:
        if b.osm_type == "way":
            add_way(b.osm_id, tags)
        else:
            rel = copy.deepcopy(index[("relation", b.osm_id)])
            _set_tags(rel, tags)
            rel.set("action", "modify")
            relations[b.osm_id] = rel
            for m in rel.findall("member"):
                if m.attrib["type"] == "way" and int(m.attrib["ref"]) not in ways:
                    add_way(int(m.attrib["ref"]))

    root = ET.Element("osm", version="0.6", generator="osm3d")
    if upload != "true":  # "true" é o comportamento por omissão do JOSM: sem atributo
        root.set("upload", upload)
    for group in (nodes, ways, relations):
        for key in sorted(group):
            root.append(group[key])
    ET.indent(root)
    out.parent.mkdir(parents=True, exist_ok=True)
    ET.ElementTree(root).write(out, encoding="UTF-8", xml_declaration=True)
    return out


def write_report(records: list[ExportRecord], path: Path) -> Path:
    def fmt(d: dict[str, object]) -> str:
        return ";".join(f"{k}={v}" for k, v in sorted(d.items()))

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["osm_key", "exportado", "motivo", "adicionadas", "alteradas", "conflitos"])
        for r in records:
            w.writerow(
                [
                    r.key,
                    r.exported,
                    r.reason,
                    fmt(dict(r.added)),
                    fmt({k: f"{a}->{b}" for k, (a, b) in r.changed.items()}),
                    fmt({k: f"OSM {a} / proposto {b}" for k, (a, b) in r.conflicts.items()}),
                ]
            )
    return path
