"""Ajustes à geometria de elementos que já existem no OSM (``landuse``, edifícios).

Usado quando um edifício novo de contorno fiável (cartografia municipal) sobrepõe uma
lasca de um ``landuse`` ou de um edifício do OSM: o edifício fica onde está e é o
elemento do OSM que passa a contorná-lo (ver :mod:`osm3d.validation.topology`).

Para editar é preciso a versão atual de cada way e dos seus nós, por isso pede-se ao
Overpass, na exportação, cada way a ajustar, os seus nós e **todas as ways que usam esses
nós** (``way(bn)``). Assim:

- os nós existentes mantêm-se: só se inserem nós novos e se tiram nós do anel;
- um nó tirado só é apagado se nenhuma outra way o usa e não tem tags;
- dois ``landuse`` colados (com nós partilhados) ajustados à volta do mesmo edifício
  ganham os mesmos nós novos onde o limite comum é cortado, e continuam colados.

Edições manuais da revisão (landuse com nós movidos, acrescentados ou tirados no mapa)
aplicam-se primeiro, sobre os dados atuais; os ajustes automáticos vêm por cima. Uma
edição manual de uma way que mudou no OSM desde o contexto não se faz.

O resultado vai no mesmo ``.osm`` do JOSM, com ``action="modify"``/``"delete"``, para
ser revisto antes do upload.
"""

from __future__ import annotations

import copy
import math
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from xml.etree import ElementTree as ET

import numpy as np
from pyproj import Transformer
from shapely.geometry import LineString, Polygon
from shapely.geometry.polygon import orient

from osm3d.validation.topology import Adjustment, TopologyRules, adjusted_feature

SAME_NODE_M = 0.001  # mesma posição (a reprojeção não é exata ao bit)
MOVED_DEG = 1e-8  # ~1 mm: nó editado à mão que mudou de posição

# Landuse editado à mão: (nós da way no OSM quando se editou, anel novo sem repetir o
# primeiro com (id do nó ou None se novo, x, y) no CRS de processamento).
ManualRing = tuple[list[int], list[tuple[int | None, float, float]]]
FIRST_NEW_ID = -1_000_001  # ids negativos longe dos do resto da exportação

EDIT_QUERY = """[out:xml][timeout:{timeout}];
way(id:{ids})->.w;
node(w.w)->.n;
way(bn.n)->.p;
(.p;node(w.p););
out meta;
"""


def edit_query(way_ids: list[int], timeout_s: int = 90) -> str:
    return EDIT_QUERY.format(ids=",".join(str(i) for i in sorted(set(way_ids))), timeout=timeout_s)


def fetch_for_edit(
    way_ids: list[int],
    overpass_url: str,
    progress: Callable[[str], None] | None = None,
) -> bytes:
    """Ways a ajustar, os seus nós e as ways que partilham esses nós (``out meta``)."""
    from osm3d.acquisition.osm import _check_payload, overpass_post

    def validate(resp):  # type: ignore[no-untyped-def]
        _check_payload(resp.content)
        return resp.content

    content: bytes = overpass_post(
        edit_query(way_ids), overpass_url, validate, read_timeout_s=150, rounds=3, progress=progress
    )
    return content


@dataclass
class OsmEdits:
    ways: dict[int, ET.Element] = field(default_factory=dict)  # ways alteradas (completas)
    nodes: dict[int, ET.Element] = field(default_factory=dict)  # nós usados/novos/apagados
    new_ways: list[ET.Element] = field(default_factory=list)  # muros novos
    applied: dict[str, list[str]] = field(default_factory=dict)  # edifício → ajustes feitos
    failed: dict[str, list[str]] = field(default_factory=dict)  # edifício → ajustes falhados
    way_notes: dict[int, list[str]] = field(default_factory=dict)  # way → o que mudou


class _NodeIndex:
    """Nós (existentes e novos) por posição, para reutilizar o mesmo nó no mesmo sítio."""

    def __init__(self) -> None:
        self.grid: dict[tuple[int, int], list[tuple[int, float, float]]] = defaultdict(list)

    def _cell(self, x: float, y: float) -> tuple[int, int]:
        return math.floor(x / SAME_NODE_M), math.floor(y / SAME_NODE_M)

    def add(self, nid: int, x: float, y: float) -> None:
        self.grid[self._cell(x, y)].append((nid, x, y))

    def find(self, x: float, y: float, prefer: set[int]) -> int | None:
        cx, cy = self._cell(x, y)
        found = [
            nid
            for gx in (cx - 1, cx, cx + 1)
            for gy in (cy - 1, cy, cy + 1)
            for nid, nx, ny in self.grid.get((gx, gy), ())
            if math.hypot(nx - x, ny - y) <= SAME_NODE_M
        ]
        if not found:
            return None
        mine = [n for n in found if n in prefer]  # os da própria way primeiro
        return (mine or found)[0]


def plan_edits(
    adjustments: list[Adjustment],
    osm_xml: bytes,
    crs: str,
    rules: TopologyRules,
    *,
    skip_ways: set[int] | None = None,
    versions: dict[int, int] | None = None,
    lines: list[tuple[str, LineString, dict[str, str]]] | None = None,
    manual: dict[int, ManualRing] | None = None,
    keep_nodes: set[int] | None = None,
) -> OsmEdits:
    """Novas geometrias das ways a ajustar, a partir dos dados atuais do OSM.

    ``lines``: (chave, linha, tags) de ways novas (muros), com os nós partilhados com as
    ways ajustadas onde coincidem (a menos de 1 mm).

    ``skip_ways``: ways que não se ajustam (p.ex. contornos a que a exportação liga
    ``building:part``). ``versions``: ways que a exportação também altera (tags 3D),
    com a versão em que as tags foram calculadas; só se ajustam se o OSM ainda está
    nessa versão (as duas alterações juntam-se na mesma way).

    ``manual``: landuse editados à mão na revisão (ver :data:`ManualRing`), aplicados
    antes dos ajustes automáticos.

    ``keep_nodes``: nós existentes que vão no ficheiro mesmo sem mudarem (p.ex. os nós
    do OSM a que um edifício novo foi ligado no mapa), na versão atual.
    """
    edits = OsmEdits()
    root = ET.fromstring(osm_xml)
    node_el = {int(n.attrib["id"]): n for n in root.iter("node")}
    way_el = {int(w.attrib["id"]): w for w in root.iter("way")}
    to_crs = Transformer.from_crs("EPSG:4326", crs, always_xy=True)
    to_wgs = Transformer.from_crs(crs, "EPSG:4326", always_xy=True)
    original_refs = {
        wid: [int(nd.attrib["ref"]) for nd in w.findall("nd")] for wid, w in way_el.items()
    }
    next_id = FIRST_NEW_ID
    manual_refs: dict[int, list[int]] = {}
    # Nós novos das edições à mão: no mesmo ponto em duas ways (um vértice ligado a dois
    # elementos no mapa), o mesmo nó.
    manual_new = _NodeIndex()
    for wid, (before, hand_ring) in sorted((manual or {}).items()):
        why = _manual_problem(wid, before, hand_ring, original_refs, node_el)
        if why is not None:
            edits.failed.setdefault(f"way/{wid}", []).append(f"edição manual não feita: {why}")
            continue
        refs_new: list[int] = []
        for nid, x, y in hand_ring:
            lon, lat = to_wgs.transform(x, y)
            if nid is None and (found := manual_new.find(x, y, set())) is not None:
                nid = found
            elif nid is None:
                nid, next_id = next_id, next_id - 1
                manual_new.add(nid, x, y)
                node_el[nid] = ET.Element(
                    "node", id=str(nid), action="modify", visible="true",
                    lat=f"{lat:.7f}", lon=f"{lon:.7f}",
                )  # fmt: skip
            else:
                el = node_el[nid]
                if (
                    abs(float(el.attrib["lon"]) - lon) > MOVED_DEG
                    or abs(float(el.attrib["lat"]) - lat) > MOVED_DEG
                ):
                    el.set("lat", f"{lat:.7f}")
                    el.set("lon", f"{lon:.7f}")
                    el.set("action", "modify")
            refs_new.append(nid)
        manual_refs[wid] = [*refs_new, refs_new[0]]
    ids = list(node_el)
    xs, ys = to_crs.transform(
        np.array([float(node_el[i].attrib["lon"]) for i in ids]),
        np.array([float(node_el[i].attrib["lat"]) for i in ids]),
    )
    xy = {
        nid: (float(x), float(y))
        for nid, x, y in zip(ids, np.atleast_1d(xs), np.atleast_1d(ys), strict=True)
    }
    index = _NodeIndex()
    for nid, (x, y) in xy.items():
        index.add(nid, x, y)
    parents: dict[int, set[int]] = defaultdict(set)
    refs_of: dict[int, list[int]] = {}
    for wid in way_el:
        refs_of[wid] = manual_refs.get(wid, original_refs[wid])
        for r in original_refs[wid]:
            parents[r].add(wid)

    by_way: dict[int, list[Adjustment]] = defaultdict(list)
    for a in adjustments:
        by_way[a.feature.osm_id].append(a)

    def adjust(wid: int, adjs: list[Adjustment]) -> Polygon | str:
        refs = refs_of.get(wid)
        if wid in (skip_ways or set()):
            return "a way leva building:part nesta exportação"
        if refs is None:
            return "a way já não existe no OSM"
        if versions and wid in versions and int(way_el[wid].attrib["version"]) != versions[wid]:
            return "a way mudou no OSM desde a fase fetch: correr a fase fetch de novo"
        if len(refs) < 4 or refs[0] != refs[-1] or any(r not in xy for r in refs):
            return "a way não é um anel fechado completo"
        current = Polygon([xy[r] for r in refs])
        if not current.is_valid:
            return "a geometria atual no OSM é inválida"
        width = max((a.max_width_m or 0.0 for a in adjs), default=0.0) or None
        new = adjusted_feature(current, [(a.obstacle, a.include) for a in adjs], rules, width)
        if new is None:
            return "no OSM atual o ajuste já não é uma lasca"
        return orient(new, 1.0 if current.exterior.is_ccw else -1.0)

    # Os ajustes de landuse de um edifício são tudo ou nada: se o landuse onde ele entra
    # não se ajusta, alargar o outro sobrepunha os dois. Numa way que não se consegue
    # ajustar, falham os edifícios que não passam sozinhos (ou todos, se passam todos).
    # Repete-se sem os falhados até estabilizar.
    failed: dict[tuple[str, str], str] = {}
    while True:
        planned: dict[int, tuple[Polygon, list[Adjustment]]] = {}
        newly = False
        for wid, adjs in sorted(by_way.items()):
            active = [a for a in adjs if a.group not in failed]
            if not active:
                continue
            res = adjust(wid, active)
            if not isinstance(res, str):
                planned[wid] = (res, active)
                continue
            alone = {id(a): adjust(wid, [a]) for a in active}
            culprits = [a for a in active if isinstance(alone[id(a)], str)] or active
            for a in culprits:
                why = alone[id(a)] if isinstance(alone[id(a)], str) else res
                failed.setdefault(a.group, f"{a.feature.label}: {why}")
            newly = True
        if not newly:
            break
    for a in adjustments:
        if a.group in failed:
            edits.failed.setdefault(a.building_key, []).append(
                f"{a.description} — não ajustado ({failed[a.group]})"
            )

    new_nodes: dict[int, tuple[float, float]] = {}
    new_refs: dict[int, list[int]] = {}
    for wid, (new, adjs) in planned.items():
        refs = refs_of[wid]
        own = set(refs)
        ring: list[int] = []
        for x, y in list(new.exterior.coords)[:-1]:
            found = index.find(x, y, own)
            if found is None:
                nid, next_id = next_id, next_id - 1
                index.add(nid, x, y)
                new_nodes[nid] = (x, y)
            else:
                nid = found
            if not ring or ring[-1] != nid:
                ring.append(nid)
        if len(ring) > 1 and ring[0] == ring[-1]:
            ring.pop()
        if refs[0] in ring:  # mesmo nó inicial: diferença mínima no histórico
            k = ring.index(refs[0])
            ring = ring[k:] + ring[:k]
        new_refs[wid] = [*ring, ring[0]]
        for a in adjs:
            edits.applied.setdefault(a.building_key, []).append(a.description)
        edits.way_notes[wid] = [a.description for a in adjs]
    for wid, refs in manual_refs.items():  # editados à mão (e sem ajustes automáticos)
        new_refs.setdefault(wid, refs)
        edits.way_notes.setdefault(wid, []).insert(0, "geometria editada à mão na revisão")

    # Ways novas (muros): depois dos anéis, para usarem os nós deles.
    new_line_refs: list[tuple[list[int], dict[str, str]]] = []
    for _key, line, line_tags in lines or []:
        refs_line: list[int] = []
        for x, y in line.coords:
            found = index.find(x, y, set())
            if found is None:
                nid, next_id = next_id, next_id - 1
                index.add(nid, x, y)
                new_nodes[nid] = (x, y)
            else:
                nid = found
            if not refs_line or refs_line[-1] != nid:
                refs_line.append(nid)
        if len(refs_line) >= 2:
            new_line_refs.append((refs_line, line_tags))
    way_id = FIRST_NEW_ID * 2  # ids das ways novas, longe dos nós
    for refs_line, line_tags in new_line_refs:
        el = ET.Element("way", id=str(way_id), action="modify", visible="true")
        way_id -= 1
        for r in refs_line:
            ET.SubElement(el, "nd", ref=str(r))
        for key, value in sorted(line_tags.items()):
            ET.SubElement(el, "tag", k=key, v=value)
        edits.new_ways.append(el)

    used = {r for rs in new_refs.values() for r in rs} | {r for rs, _ in new_line_refs for r in rs}
    for wid, rs in new_refs.items():
        el = copy.deepcopy(way_el[wid])
        for nd in el.findall("nd"):
            el.remove(nd)
        tags = el.findall("tag")
        for t in tags:
            el.remove(t)
        for r in rs:
            ET.SubElement(el, "nd", ref=str(r))
        for t in tags:  # as tags ficam depois dos nós, como no formato OSM
            el.append(t)
        el.set("action", "modify")
        edits.ways[wid] = el
    for r in used:
        if r in new_nodes:
            lon, lat = to_wgs.transform(*new_nodes[r])
            edits.nodes[r] = ET.Element(
                "node",
                id=str(r),
                action="modify",
                visible="true",
                lat=f"{lat:.7f}",
                lon=f"{lon:.7f}",
            )
        else:
            edits.nodes[r] = copy.deepcopy(node_el[r])
    # Nós que saíram do anel: apagam-se se mais nada os usa (nem têm tags).
    dropped = {r for wid in new_refs for r in original_refs[wid]} - used
    for r in dropped:
        others = parents[r] - set(new_refs)
        if not others and node_el[r].find("tag") is None:
            el = copy.deepcopy(node_el[r])
            el.set("action", "delete")
            edits.nodes[r] = el
    for r in keep_nodes or ():
        if r in node_el and r not in edits.nodes:
            edits.nodes[r] = copy.deepcopy(node_el[r])
    return edits


def _manual_problem(
    wid: int,
    before: list[int],
    ring: list[tuple[int | None, float, float]],
    refs: dict[int, list[int]],
    node_el: dict[int, ET.Element],
) -> str | None:
    """Porque é que a edição manual de ``wid`` não se pode aplicar (ou ``None``)."""
    if wid not in refs:
        return "a way já não existe no OSM"
    if refs[wid] != before:
        return "a way mudou no OSM desde a edição: rever e editar de novo"
    if len(ring) < 3:
        return "o anel editado tem menos de 3 nós"
    missing = [n for n, _, _ in ring if n is not None and n not in node_el]
    if missing:
        return f"nós que já não existem no OSM: {missing[:3]}"
    if not Polygon([(x, y) for _, x, y in ring]).is_valid:
        return "o anel editado não é um polígono válido"
    return None
