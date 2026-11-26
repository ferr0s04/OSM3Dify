"""Edições manuais de geometria feitas na revisão (ao estilo do editor iD do OSM).

Três tipos de elemento editáveis no mapa:

- **landuse e edifícios do OSM** (ways fechadas do contexto): como no iD, editam-se
  **nós**. Mover um nó move-o em todas as ways que o usam (dois landuse colados, ou um
  edifício encostado a outro, continuam colados); a way pode também ganhar nós novos ou
  perder nós. Guarda-se:

  - ``node/<id>`` → posição nova (lon, lat) de um nó existente do OSM;
  - ``way/<id>``  → o anel da way quando a sequência de nós muda (nós acrescentados
    ou tirados): ``before`` (os nós no OSM quando se editou) e ``ring`` (id do nó ou
    ``None`` para um nó novo, lon, lat), sem repetir o primeiro.

- **muros propostos**: ``wall/<id>`` → a linha toda (lon, lat).

- **edifícios novos** (cartografia municipal): ``new/<id>`` → o contorno (lon, lat), sem
  repetir o primeiro vértice.

O contexto OSM com estas edições (:func:`apply_to_context`) é o que a revisão mostra e o
que se usa nas ligações dos muros ao landuse e na topologia dos edifícios novos. Na
exportação, as edições aplicam-se aos dados atuais do OSM (ver
:func:`osm3d.export.osm_edits.plan_edits`); se a way mudou no OSM entretanto, a edição
não se faz e fica no relatório.
"""

from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import dataclass, field
from typing import Any

LonLat = tuple[float, float]
RingNode = tuple[int | None, float, float]  # (id do nó ou None se novo, lon, lat)

SAME_POSITION_DEG = 1e-7  # ~1 cm: abaixo disto o nó não se moveu (arredondamentos)


def same_position(a: LonLat, b: LonLat) -> bool:
    return abs(a[0] - b[0]) <= SAME_POSITION_DEG and abs(a[1] - b[1]) <= SAME_POSITION_DEG


def new_node_key(lon: float, lat: float) -> tuple[float, float]:
    """Posição de um nó novo arredondada a ~1 cm: o mesmo ponto em duas ways é o mesmo nó
    (o editor copia as coordenadas exatas ao ligar um vértice a outro elemento)."""
    return (round(lon, 7), round(lat, 7))


@dataclass
class LanduseRing:
    before: list[int]  # nós da way no OSM quando se editou (anel fechado)
    ring: list[RingNode]  # anel novo, sem repetir o primeiro

    def to_json(self) -> dict[str, Any]:
        return {"before": self.before, "ring": [list(n) for n in self.ring]}

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> LanduseRing:
        return cls(
            [int(i) for i in d["before"]],
            [(None if n[0] is None else int(n[0]), float(n[1]), float(n[2])) for n in d["ring"]],
        )


@dataclass
class GeometryEdits:
    nodes: dict[int, LonLat] = field(default_factory=dict)
    ways: dict[int, LanduseRing] = field(default_factory=dict)
    walls: dict[str, list[LonLat]] = field(default_factory=dict)
    buildings: dict[str, list[LonLat]] = field(default_factory=dict)  # "new/<id>" → anel

    @property
    def landuse_empty(self) -> bool:
        """Sem edições do contexto OSM (nós ou anéis de landuse/edifícios do OSM)."""
        return not self.nodes and not self.ways

    def signature(self) -> str:
        """Muda sempre que as edições mudam (para invalidar caches)."""
        raw = json.dumps(
            {
                "n": sorted((k, v) for k, v in self.nodes.items()),
                "w": sorted((k, v.to_json()) for k, v in self.ways.items()),
                "m": sorted(self.walls.items()),
                "b": sorted(self.buildings.items()),
            },
            sort_keys=True,
        )
        return hashlib.sha1(raw.encode()).hexdigest()[:16]

    def records(self) -> dict[str, Any]:
        """(chave → dados) para guardar."""
        out: dict[str, Any] = {f"node/{k}": list(v) for k, v in self.nodes.items()}
        out |= {f"way/{k}": v.to_json() for k, v in self.ways.items()}
        out |= {k: [list(c) for c in v] for k, v in self.walls.items()}
        out |= {k: [list(c) for c in v] for k, v in self.buildings.items()}
        return out

    @classmethod
    def from_records(cls, records: dict[str, Any]) -> GeometryEdits:
        edits = cls()
        for key, data in records.items():
            kind, _, ident = key.partition("/")
            if kind == "node":
                edits.nodes[int(ident)] = (float(data[0]), float(data[1]))
            elif kind == "way":
                edits.ways[int(ident)] = LanduseRing.from_json(data)
            elif kind == "wall":
                edits.walls[key] = [(float(c[0]), float(c[1])) for c in data]
            elif kind == "new":
                edits.buildings[key] = [(float(c[0]), float(c[1])) for c in data]
        return edits


# ----------------------------------------------------------------- contexto OSM


@dataclass
class ContextIndex:
    """Nós e ways do contexto OSM (JSON do Overpass, ``out geom``)."""

    nodes: dict[int, LonLat]
    refs: dict[int, list[int]]  # way → nós (anel fechado repete o primeiro)
    tags: dict[int, dict[str, str]]

    @classmethod
    def of(cls, data: dict[str, Any]) -> ContextIndex:
        nodes: dict[int, LonLat] = {}
        refs: dict[int, list[int]] = {}
        tags: dict[int, dict[str, str]] = {}
        for el in data.get("elements", []):
            ids, geom = el.get("nodes"), el.get("geometry")
            if el.get("type") != "way" or not ids or not geom or len(ids) != len(geom):
                continue
            wid = int(el["id"])
            refs[wid] = [int(i) for i in ids]
            tags[wid] = el.get("tags", {})
            for nid, p in zip(ids, geom, strict=True):
                if p:
                    nodes[int(nid)] = (float(p["lon"]), float(p["lat"]))
        return cls(nodes, refs, tags)

    def _closed_with(self, way_id: int, keys: tuple[str, ...]) -> bool:
        r = self.refs.get(way_id)
        tags = self.tags.get(way_id, {})
        return bool(r and any(k in tags for k in keys) and len(r) >= 4 and r[0] == r[-1])

    def is_landuse(self, way_id: int) -> bool:
        return self._closed_with(way_id, ("landuse",))

    def is_building(self, way_id: int) -> bool:
        return self._closed_with(way_id, ("building",))

    def is_area(self, way_id: int) -> bool:
        """Way fechada editável no mapa: landuse ou edifício."""
        return self._closed_with(way_id, ("landuse", "building"))

    def parents(self, node_id: int) -> list[int]:
        return [w for w, r in self.refs.items() if node_id in r]


def landuse_ring(ctx: ContextIndex, edits: GeometryEdits, way_id: int) -> list[RingNode]:
    """Anel atual (com as edições) de uma way fechada (landuse ou edifício), sem repetir o
    primeiro nó."""

    def position(nid: int, default: LonLat) -> LonLat:
        return edits.nodes.get(nid) or ctx.nodes.get(nid, default)

    if way_id in edits.ways:
        out: list[RingNode] = []
        for nid, lon, lat in edits.ways[way_id].ring:
            out.append((nid, lon, lat) if nid is None else (nid, *position(nid, (lon, lat))))
        return out
    return [(i, *position(i, ctx.nodes[i])) for i in ctx.refs[way_id][:-1]]


def is_edited(ctx: ContextIndex, edits: GeometryEdits, way_id: int) -> bool:
    return way_id in edits.ways or any(i in edits.nodes for i in ctx.refs.get(way_id, []))


def edited_landuse(ctx: ContextIndex, edits: GeometryEdits) -> list[int]:
    """Landuse com alguma edição (anel novo ou um nó movido)."""
    return [w for w in edited_areas(ctx, edits) if ctx.is_landuse(w)]


def edited_areas(ctx: ContextIndex, edits: GeometryEdits) -> list[int]:
    """Landuse e edifícios do OSM com alguma edição (anel novo ou um nó movido)."""
    moved = set(edits.nodes)
    return sorted(
        w
        for w in ctx.refs
        if ctx.is_area(w) and (w in edits.ways or moved.intersection(ctx.refs[w]))
    )


def set_landuse(
    ctx: ContextIndex, edits: GeometryEdits, way_id: int, ring: list[RingNode]
) -> GeometryEdits:
    """Edições com a way fechada ``way_id`` (landuse ou edifício) a passar a ``ring``
    (validado à parte).

    Os nós existentes que mudaram de posição ficam movidos (em todas as ways que os
    usam); os que voltaram à posição do OSM deixam de estar. O anel só se guarda se a
    sequência de nós mudou.
    """
    if not ctx.is_area(way_id):
        raise ValueError(f"way/{way_id} não é um landuse ou edifício fechado do contexto OSM")
    out = copy.deepcopy(edits)
    for nid, lon, lat in ring:
        if nid is None:
            continue
        if nid not in ctx.nodes:
            raise ValueError(f"o nó {nid} não está no contexto OSM")
        if not same_position((lon, lat), ctx.nodes[nid]):
            out.nodes[nid] = (lon, lat)
        else:
            out.nodes.pop(nid, None)
    ids = [n[0] for n in ring]
    if None not in ids and ids == ctx.refs[way_id][:-1]:
        out.ways.pop(way_id, None)
    else:
        out.ways[way_id] = LanduseRing(list(ctx.refs[way_id]), list(ring))
    return out


def reset_landuse(ctx: ContextIndex, edits: GeometryEdits, way_id: int) -> GeometryEdits:
    """Repõe a way (landuse ou edifício) como no OSM (também os seus nós nas ways
    vizinhas)."""
    out = copy.deepcopy(edits)
    out.ways.pop(way_id, None)
    for nid in ctx.refs.get(way_id, []):
        out.nodes.pop(nid, None)
    return out


def apply_to_context(data: dict[str, Any], edits: GeometryEdits) -> dict[str, Any]:
    """Cópia do contexto OSM com as edições (nós movidos em todas as ways e anéis novos)."""
    if edits.landuse_empty:
        return data
    ctx = ContextIndex.of(data)
    out = dict(data)
    elements = []
    # Nós novos: ids negativos (só para esta cópia). Dois nós novos no mesmo ponto (um
    # vértice ligado a dois elementos, como no iD) são o mesmo nó.
    new_ids: dict[tuple[float, float], int] = {}
    for el in data.get("elements", []):
        if el.get("type") != "way" or not el.get("nodes"):
            elements.append(el)
            continue
        wid = int(el["id"])
        if wid in edits.ways and ctx.is_area(wid):
            ring = landuse_ring(ctx, edits, wid)
            ids = []
            for i, lon, lat in ring:
                if i is None:
                    i = new_ids.setdefault(new_node_key(lon, lat), -1 - len(new_ids))
                ids.append(i)
            el = {
                **el,
                "nodes": [*ids, ids[0]],
                "geometry": [{"lon": lon, "lat": lat} for _, lon, lat in [*ring, ring[0]]],
            }
        elif edits.nodes.keys() & set(el["nodes"]):
            geom = [
                {"lon": edits.nodes[i][0], "lat": edits.nodes[i][1]} if i in edits.nodes else p
                for i, p in zip(el["nodes"], el.get("geometry", []), strict=False)
            ]
            el = {**el, "geometry": geom}
        elements.append(el)
    out["elements"] = elements
    return out
