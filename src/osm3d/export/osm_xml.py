"""Exportação para ficheiro ``.osm`` no formato JOSM, para revisão humana.

Regras:

- Os elementos modificados levam ``action="modify"`` e mantêm ``id`` e ``version``
  originais. Os nós e as ways-membro vão sem alterações, só para o JOSM conseguir
  desenhar e validar a geometria. Este módulo nunca publica.
- As tags são juntas com :func:`osm3d.export.s3db_tags.merge_tags`: tags mapeadas por
  humanos não são sobrescritas (salvo opção explícita) e os conflitos vão para o
  relatório.
- O atributo ``upload`` do ficheiro vem de ``export.josm_upload`` (por omissão
  ``false``: o JOSM avisa antes do upload; ``never`` recusa-o). O upload é sempre feito
  por uma pessoa, no JOSM.
- ``building:part``: cada parte é uma way nova ligada ao contorno (ver ``_attach_parts``).
- Edifícios em falta no OSM (da cartografia municipal, ``new/<id>``) aceites: way nova
  com nós novos (ids negativos), a tag ``building`` e as tags S3DB.
"""

from __future__ import annotations

import copy
import csv
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal
from xml.etree import ElementTree as ET

import numpy as np
from pyproj import Transformer
from shapely.geometry import Polygon

from osm3d.domain.models import Building, BuildingPart, PartsLayout
from osm3d.export.osm_edits import OsmEdits
from osm3d.export.s3db_tags import TagPolicy, format_metres, merge_tags, part_to_tags
from osm3d.parts import SNAP_M, on_segment

# Vértice de um edifício novo ligado no mapa a um nó do OSM: o mesmo ponto (≤ 1 mm).
SAME_EXISTING_NODE_M = 0.001

UploadPolicy = Literal["never", "false", "true"]


@dataclass
class ExportRecord:
    key: str
    exported: bool
    reason: str
    added: dict[str, str] = field(default_factory=dict)
    changed: dict[str, tuple[str, str]] = field(default_factory=dict)
    conflicts: dict[str, tuple[str, str]] = field(default_factory=dict)
    # Edifícios novos: recortes feitos e sobreposições/cruzamentos por resolver.
    topology: str = ""


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
        if b.review_archived:
            reason = "arquivado (já submetido ou rejeitado)"
        elif b.review_status == "rejected":
            reason = "rejeitado na revisão"
        elif not accepted and not include_pending:
            reason = "por rever"
        elif not accepted and b.needs_review:
            reason = "sinalizado: " + ",".join(f.code for f in b.qa_flags if f.severity != "info")
        elif not b.parts:
            reason = "sem partes"
        elif (has_parts(b) or is_split(b)) and b.osm_type not in ("way", "new"):
            reason = "partes só em contornos simples (way), não em multipolígonos"
        elif len(b.parts) > 1 and not has_parts(b) and not is_split(b):
            reason = "várias partes sem divisão definida"
        if reason:
            records.append(ExportRecord(b.key, False, reason))
            continue
        merge = merge_tags(b.existing_tags, outline_tags(b, policy), overwrite=overwrite)
        # Partes novas (ou a divisão em vários edifícios) são sempre alteração.
        changed = merge.has_changes or has_parts(b) or is_split(b)
        ok = (
            f"ok ({len(b.parts)} partes)"
            if has_parts(b)
            else f"ok (dividido em {len(b.parts)} edifícios)"
            if is_split(b)
            else "ok"
        )
        record = ExportRecord(
            b.key,
            changed,
            ok if changed else "sem alterações",
            merge.added,
            merge.changed,
            merge.conflicts,
        )
        records.append(record)
        if changed:
            selected.append((b, merge.tags))
    return selected, records


def _divided(b: Building) -> bool:
    return b.layout is not None and len(b.parts) == len(b.layout.parts) and len(b.parts) > 1


def has_parts(b: Building) -> bool:
    """Dividido em ``building:part`` dentro do contorno."""
    return _divided(b) and b.layout is not None and b.layout.split == "parts"


def is_split(b: Building) -> bool:
    """Dividido em vários edifícios (cada peça um edifício com as suas tags 3D)."""
    return _divided(b) and b.layout is not None and b.layout.split == "buildings"


# Tags que não passam do edifício original para as peças novas de uma divisão em vários
# edifícios: as 3D (cada peça tem as suas).
_SPLIT_DROP = {"height", "min_height", "building:levels", "building:min_level", "building:part"}


def split_piece_tags(
    b: Building, i: int, policy: TagPolicy, base: dict[str, str]
) -> dict[str, str]:
    """Tags da peça ``i`` (≥ 1) de um edifício dividido em vários: as do edifício original
    (``base``, já com as finais) sem as 3D, mais as 3D da própria peça."""
    keep = {k: v for k, v in base.items() if k not in _SPLIT_DROP and not k.startswith("roof:")}
    return {**keep, **part_to_tags(b.parts[i], policy)}


def outline_tags(b: Building, policy: TagPolicy) -> dict[str, str]:
    """Tags propostas para o contorno.

    Com partes, o contorno leva só a altura total (a da parte mais alta). A forma
    e as cores ficam em cada ``building:part``. Dividido em vários edifícios, o
    contorno (a way original) fica com a 1.ª peça e as suas tags.
    """
    if not has_parts(b):
        return part_to_tags(b.parts[0], policy)
    top = max(p.height.value for p in b.parts)
    return {"height": format_metres(top, policy.height_rounding_m)}


def part_way_tags(part: BuildingPart, policy: TagPolicy) -> dict[str, str]:
    return {"building:part": "yes", **part_to_tags(part, policy)}


class _NodeIds:
    """Ids negativos (elementos novos no JOSM), únicos no ficheiro."""

    def __init__(self) -> None:
        self.next = -1

    def new(self) -> int:
        nid = self.next
        self.next -= 1
        return nid


def _attach_parts(
    way: ET.Element,
    b: Building,
    index: dict[tuple[str, int], ET.Element],
    nodes: dict[int, ET.Element],
    new_ways: list[ET.Element],
    ids: _NodeIds,
    policy: TagPolicy,
    crs: str,
) -> None:
    """Acrescenta as ``building:part`` de ``b`` ligadas topologicamente ao contorno.

    - vértice que coincide com um nó do contorno → esse nó;
    - vértice sobre um lado do contorno → nó novo, **inserido nesse lado do contorno**;
    - vértice interior → nó novo, partilhado pelas partes que o usam.
    """
    ring, vertex_node, inserts = _layout_nodes(way, b, index, nodes, ids, crs)

    if inserts:
        for nd in way.findall("nd"):
            way.remove(nd)
        new_ring: list[int] = []
        for i, ref in enumerate(ring):
            new_ring.append(ref)
            new_ring += [nid for _, nid in sorted(inserts.get(i, []))]
        for ref in [*new_ring, new_ring[0]]:
            ET.SubElement(way, "nd", ref=str(ref))
        # As tags ficam depois dos nós, como no formato OSM.
        for tag in way.findall("tag"):
            way.remove(tag)
            way.append(tag)
        way.set("action", "modify")

    assert b.layout is not None
    for k, part in enumerate(b.parts):
        el = ET.Element("way", id=str(ids.new()), action="modify", visible="true")
        part_ring = [vertex_node[i] for i in b.layout.parts[k]]
        for ref in [*part_ring, part_ring[0]]:
            ET.SubElement(el, "nd", ref=str(ref))
        for key, value in sorted(part_way_tags(part, policy).items()):
            ET.SubElement(el, "tag", k=key, v=value)
        new_ways.append(el)


def _split_way(
    way: ET.Element,
    b: Building,
    index: dict[tuple[str, int], ET.Element],
    nodes: dict[int, ET.Element],
    new_ways: list[ET.Element],
    ids: _NodeIds,
    policy: TagPolicy,
    crs: str,
) -> None:
    """Edifício do OSM dividido em vários edifícios: a way fica com a 1.ª peça (a sua
    história e as tags finais) e as outras peças são ways novas com as tags do edifício
    sem as 3D, mais as suas. Os nós do contorno mantêm-se; as peças vizinhas partilham
    os nós da parede comum."""
    assert b.layout is not None
    _, vertex_node, _ = _layout_nodes(way, b, index, nodes, ids, crs)
    base = {t.attrib["k"]: t.attrib["v"] for t in way.findall("tag")}
    first = [vertex_node[i] for i in b.layout.parts[0]]
    _replace_nodes(way, [str(r) for r in [*first, first[0]]])
    way.set("action", "modify")
    for k in range(1, len(b.parts)):
        el = ET.Element("way", id=str(ids.new()), action="modify", visible="true")
        piece = [vertex_node[i] for i in b.layout.parts[k]]
        for ref in [*piece, piece[0]]:
            ET.SubElement(el, "nd", ref=str(ref))
        for key, value in sorted(split_piece_tags(b, k, policy, base).items()):
            ET.SubElement(el, "tag", k=key, v=value)
        new_ways.append(el)


def _layout_nodes(
    way: ET.Element,
    b: Building,
    index: dict[tuple[str, int], ET.Element],
    nodes: dict[int, ET.Element],
    ids: _NodeIds,
    crs: str,
) -> tuple[list[int], list[int], dict[int, list[tuple[float, int]]]]:
    """Nó de cada vértice da divisão: o do contorno no mesmo ponto, ou um nó novo (com o
    lado do contorno onde fica, se fica sobre um). Devolve (anel do contorno, nó de cada
    vértice, inserções por lado)."""
    assert b.layout is not None
    to_crs = Transformer.from_crs("EPSG:4326", crs, always_xy=True)
    to_wgs = Transformer.from_crs(crs, "EPSG:4326", always_xy=True)
    refs = [int(nd.attrib["ref"]) for nd in way.findall("nd")]
    ring = refs[:-1] if len(refs) > 1 and refs[0] == refs[-1] else refs
    lonlat = np.array(
        [
            [float(index[("node", r)].attrib["lon"]), float(index[("node", r)].attrib["lat"])]
            for r in ring
        ]
    )
    ox, oy = to_crs.transform(lonlat[:, 0], lonlat[:, 1])
    outline = np.column_stack([ox, oy])

    vertex_node: list[int] = []
    inserts: dict[int, list[tuple[float, int]]] = {}
    for v in b.layout.vertices:
        p = np.asarray(v, float)
        dist = np.linalg.norm(outline - p, axis=1)
        if float(dist.min()) <= SNAP_M:
            vertex_node.append(ring[int(dist.argmin())])
            continue
        nid = ids.new()
        lon, lat = to_wgs.transform(p[0], p[1])
        nodes[nid] = ET.Element(
            "node", id=str(nid), action="modify", visible="true", lat=f"{lat:.9f}", lon=f"{lon:.9f}"
        )
        vertex_node.append(nid)
        for i in range(len(ring)):
            t = on_segment(p, outline[i], outline[(i + 1) % len(ring)], SNAP_M)
            if t is not None:
                inserts.setdefault(i, []).append((t, nid))
                break
    return ring, vertex_node, inserts


def _add_new_buildings(
    selected: list[tuple[Building, dict[str, str]]],
    nodes: dict[int, ET.Element],
    new_ways: list[ET.Element],
    ids: _NodeIds,
    crs: str,
    policy: TagPolicy,
    existing: list[tuple[int, float, float]] | None = None,
) -> None:
    """Edifícios em falta no OSM (candidatos aceites): nós e ways novos (ids negativos).

    Candidatos contíguos (uma mancha LiDAR dividida em vários edifícios) e as
    ``building:part`` de cada um partilham nós: vértices coincidentes são o mesmo nó e
    um vértice sobre o lado de outro anel é inserido nesse lado (sem junções em T).
    ``existing``: nós do OSM (id, x, y no CRS) a que os edifícios foram ligados no mapa:
    um vértice no mesmo ponto (≤ 1 mm) usa esse nó em vez de um novo.
    No JOSM confere-se com a ortofoto antes do upload.
    """
    if not selected:
        return
    from osm3d.parts import layout_from_polygons

    to_wgs = Transformer.from_crs(crs, "EPSG:4326", always_xy=True)
    # Anéis: contornos primeiro (dividido em vários edifícios: cada peça, a 1.ª com as tags
    # finais), depois as partes de cada edifício com partes.
    rings: list[tuple[Polygon, dict[str, str]]] = []
    for b, tags in selected:
        if is_split(b):
            assert b.layout is not None
            rings += [
                (b.layout.polygon(i), tags if i == 0 else split_piece_tags(b, i, policy, tags))
                for i in range(len(b.parts))
            ]
        else:
            rings.append((b.footprint, tags))
    for b, _ in selected:
        if has_parts(b):
            assert b.layout is not None
            rings += [
                (b.layout.polygon(i), part_way_tags(part, policy)) for i, part in enumerate(b.parts)
            ]
    shared = layout_from_polygons([g for g, _ in rings])
    if len(shared.parts) != len(rings):  # anel degenerado: sem partilha, um a um
        for g, tags in rings:
            _write_rings([tags], layout_from_polygons([g]), nodes, new_ways, ids, to_wgs, existing)
        return
    _write_rings([t for _, t in rings], shared, nodes, new_ways, ids, to_wgs, existing)


def _write_rings(
    tags_per_ring: list[dict[str, str]],
    shared: PartsLayout,
    nodes: dict[int, ET.Element],
    new_ways: list[ET.Element],
    ids: _NodeIds,
    to_wgs: Transformer,
    existing: list[tuple[int, float, float]] | None = None,
) -> None:
    node_of: dict[int, int] = {}
    known = np.array([(x, y) for _, x, y in existing or []], float).reshape(-1, 2)
    for k, tags in enumerate(tags_per_ring):
        refs = []
        for v in shared.parts[k]:
            if v not in node_of and len(known):
                d = np.hypot(*(known - np.asarray(shared.vertices[v], float)).T)
                if d.min() <= SAME_EXISTING_NODE_M:
                    assert existing is not None
                    node_of[v] = existing[int(d.argmin())][0]  # nó do OSM já no ficheiro
            if v not in node_of:
                nid = ids.new()
                lon, lat = to_wgs.transform(*shared.vertices[v])
                nodes[nid] = ET.Element(
                    "node",
                    id=str(nid),
                    action="modify",
                    visible="true",
                    lat=f"{lat:.9f}",
                    lon=f"{lon:.9f}",
                )
                node_of[v] = nid
            refs.append(node_of[v])
        el = ET.Element("way", id=str(ids.new()), action="modify", visible="true")
        for ref in [*refs, refs[0]]:
            ET.SubElement(el, "nd", ref=str(ref))
        for key, value in sorted(tags.items()):
            ET.SubElement(el, "tag", k=key, v=value)
        new_ways.append(el)


def _replace_nodes(way: ET.Element, refs: list[str]) -> None:
    for nd in way.findall("nd"):
        way.remove(nd)
    tags = way.findall("tag")
    for t in tags:
        way.remove(t)
    for r in refs:
        ET.SubElement(way, "nd", ref=r)
    for t in tags:
        way.append(t)
    way.set("action", "modify")


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
    policy: TagPolicy | None = None,
    crs: str = "EPSG:3763",
    edits: OsmEdits | None = None,
) -> Path:
    """Escreve os edifícios selecionados (com as tags finais) num ``.osm`` para o JOSM.

    ``edits``: ``landuse``/edifícios do OSM ajustados à volta dos edifícios novos.
    """
    policy = policy or TagPolicy()
    index = _index(ET.parse(source_osm).getroot())
    nodes: dict[int, ET.Element] = {}
    ways: dict[int, ET.Element] = {}
    relations: dict[int, ET.Element] = {}
    new_ways: list[ET.Element] = []
    ids = _NodeIds()

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

    existing: list[tuple[int, float, float]] = []
    if edits is not None:
        # Nós do OSM no ficheiro (pedidos na exportação): os edifícios novos ligados a eles
        # no mapa usam-nos.
        to_crs = Transformer.from_crs("EPSG:4326", crs, always_xy=True)
        for nid, el in edits.nodes.items():
            if nid > 0 and el.attrib.get("action") != "delete":
                x, y = to_crs.transform(float(el.attrib["lon"]), float(el.attrib["lat"]))
                existing.append((nid, float(x), float(y)))
    _add_new_buildings(
        [(b, tags) for b, tags in selected if b.osm_type == "new"],
        nodes,
        new_ways,
        ids,
        crs,
        policy,
        existing,
    )
    for b, tags in selected:
        if b.osm_type == "new":
            continue
        if b.osm_type == "way":
            add_way(b.osm_id, tags)
            if has_parts(b):
                _attach_parts(ways[b.osm_id], b, index, nodes, new_ways, ids, policy, crs)
            elif is_split(b):
                _split_way(ways[b.osm_id], b, index, nodes, new_ways, ids, policy, crs)
        else:
            rel = copy.deepcopy(index[("relation", b.osm_id)])
            _set_tags(rel, tags)
            rel.set("action", "modify")
            relations[b.osm_id] = rel
            for m in rel.findall("member"):
                if m.attrib["type"] == "way" and int(m.attrib["ref"]) not in ways:
                    add_way(int(m.attrib["ref"]))

    if edits is not None:
        # Versões atuais (pedidas na exportação): substituem as do ficheiro da fase fetch.
        nodes.update(edits.nodes)
        for wid, el in edits.ways.items():
            if wid in ways:
                # Edifício com tags 3D e também ajustado: as tags já calculadas e os nós
                # novos (mesma versão, verificada no plano dos ajustes).
                _replace_nodes(ways[wid], [nd.attrib["ref"] for nd in el.findall("nd")])
            else:
                ways[wid] = el
    root = ET.Element("osm", version="0.6", generator="osm3d")
    if upload != "true":  # "true" é o comportamento por omissão do JOSM: sem atributo
        root.set("upload", upload)
    # Nós: existentes por id crescente, depois os novos (−1, −2, …).
    for key in sorted(k for k in nodes if k > 0):
        root.append(nodes[key])
    for key in sorted((k for k in nodes if k < 0), reverse=True):
        root.append(nodes[key])
    for key in sorted(ways):
        root.append(ways[key])
    for el in new_ways:
        root.append(el)
    for el in edits.new_ways if edits is not None else []:
        root.append(el)
    for key in sorted(relations):
        root.append(relations[key])
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
        w.writerow(
            ["osm_key", "exportado", "motivo", "adicionadas", "alteradas", "conflitos", "topologia"]
        )
        for r in records:
            w.writerow(
                [
                    r.key,
                    r.exported,
                    r.reason,
                    fmt(dict(r.added)),
                    fmt({k: f"{a}->{b}" for k, (a, b) in r.changed.items()}),
                    fmt({k: f"OSM {a} / proposto {b}" for k, (a, b) in r.conflicts.items()}),
                    r.topology,
                ]
            )
    return path
