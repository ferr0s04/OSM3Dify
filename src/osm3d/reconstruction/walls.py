"""Propostas de muros (``barrier=wall``) a partir da cartografia municipal, ligados ao
``landuse`` do OSM. Os muros de suporte são propostos como ``barrier=wall`` (nunca
``retaining_wall``), também os de caches e regras antigas; vedações e outras barreiras
não são propostas.

1. **Fonte:** as linhas das camadas municipais de muros/vedações (por prioridade: onde
   duas camadas têm o mesmo muro, a menos de ``DEDUP_M``, fica o da primeira).
2. **Já no OSM:** sai o que já está mapeado como ``barrier`` (a menos de ``DEDUP_M``) e o
   que fica dentro de edifícios do OSM.
3. **Linhas:** os troços que se tocam juntam-se (cada proposta vai de junção a junção).
4. **Ligação ao landuse:** onde o muro corre ao lado de um limite de ``landuse`` (a menos
   de ``SNAP_M`` e quase paralelo), o muro **é** esse limite: na exportação o limite passa
   para a posição do muro (a cartografia municipal é mais exata do que o OSM desenhado
   sobre imagem) e os dois partilham os nós. Um ``landuse`` colado a outro (nós
   partilhados) é ajustado da mesma forma, e continuam colados.

Cada proposta é revista (aceite/rejeitada) na aplicação, como os edifícios.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
import shapely
from pyproj import Transformer
from shapely.geometry import LineString, MultiLineString, Point, Polygon
from shapely.geometry.base import BaseGeometry
from shapely.ops import substring

from osm3d.acquisition.municipal import MunicipalBarrier

DEDUP_M = 1.5  # mesmo muro noutra camada / já mapeado no OSM
SNAP_M = 2.0  # muro a menos disto de um limite de landuse, paralelo: é esse limite
SNAP_ANGLE_DEG = 20.0
SAMPLE_M = 1.0
MIN_LEN_M = 2.0
MIN_RUN_M = 3.0  # troço mínimo ao longo de um limite para o ligar
JOIN_M = 0.3  # pontas a menos disto juntam-se


@dataclass
class LanduseRun:
    """Troço do muro que corre ao longo do limite de um landuse (way fechada)."""

    way_id: int
    label: str
    start_m: float  # ao longo do muro
    end_m: float
    max_offset_m: float
    edge: LineString  # o limite atual do landuse nesse troço (para mostrar)


@dataclass
class WallProposal:
    id: str
    tags: dict[str, str]
    geometry: LineString  # EPSG:3763, posição municipal
    layer: str
    sources: list[str]
    runs: list[LanduseRun] = field(default_factory=list)

    @property
    def key(self) -> str:
        return f"wall/{self.id}"

    def to_json(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "tags": self.tags,
            "geometry": shapely.to_wkt(self.geometry, rounding_precision=3),
            "layer": self.layer,
            "sources": self.sources,
            "runs": [
                {
                    "way_id": r.way_id,
                    "label": r.label,
                    "start_m": r.start_m,
                    "end_m": r.end_m,
                    "max_offset_m": r.max_offset_m,
                    "edge": shapely.to_wkt(r.edge, rounding_precision=3),
                }
                for r in self.runs
            ],
        }

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> WallProposal:
        runs = [
            LanduseRun(
                r["way_id"],
                r["label"],
                r["start_m"],
                r["end_m"],
                r["max_offset_m"],
                shapely.from_wkt(r["edge"]),
            )
            for r in d.get("runs", [])
        ]
        geom = shapely.from_wkt(d["geometry"])
        return cls(d["id"], d["tags"], geom, d["layer"], d["sources"], runs)


@dataclass
class OsmLanduseRing:
    way_id: int
    label: str
    polygon: Polygon


@dataclass
class WallContext:
    """O que o OSM já tem: muros, edifícios e landuse (ways fechadas), projetados."""

    barriers: list[BaseGeometry] = field(default_factory=list)
    buildings: list[BaseGeometry] = field(default_factory=list)
    landuse: list[OsmLanduseRing] = field(default_factory=list)
    has_barriers: bool = False  # contexto com muros (versão 3)


def load_wall_context(path: Path, crs: str) -> WallContext:
    data = json.loads(path.read_text(encoding="utf-8"))
    to_crs = Transformer.from_crs("EPSG:4326", crs, always_xy=True)

    def project(g: BaseGeometry) -> BaseGeometry:
        return shapely.transform(g, lambda c: np.column_stack(to_crs.transform(c[:, 0], c[:, 1])))

    ctx = WallContext(has_barriers=data.get("osm3d_context", 1) >= 3)
    for el in data.get("elements", []):
        tags = el.get("tags", {})
        if el.get("type") != "way":
            continue
        coords = [(p["lon"], p["lat"]) for p in el.get("geometry", []) if p]
        if len(coords) < 2:
            continue
        closed = len(coords) >= 4 and coords[0] == coords[-1]
        if "barrier" in tags:
            ctx.barriers.append(project(LineString(coords)))
        elif "building" in tags and closed:
            ctx.buildings.append(shapely.make_valid(project(Polygon(coords))))
        elif "landuse" in tags and closed:
            poly = project(Polygon(coords))
            if poly.is_valid:
                label = f"landuse={tags['landuse']} (way/{el['id']})"
                ctx.landuse.append(OsmLanduseRing(int(el["id"]), label, poly))
    return ctx


# ------------------------------------------------------------------ linhas


def _lines(g: BaseGeometry, min_len: float = MIN_LEN_M) -> list[LineString]:
    parts = getattr(g, "geoms", [g])
    out: list[LineString] = []
    for p in parts:
        if isinstance(p, LineString) and p.length >= min_len:
            out.append(p)
        elif isinstance(p, MultiLineString):
            out += _lines(p, min_len)
    return out


class _Cover:
    """Áreas a excluir (muros com folga, edifícios), com índice: diferença só com as perto."""

    def __init__(self, areas: list[BaseGeometry]) -> None:
        self.areas = [a for a in areas if not a.is_empty]
        self.tree = shapely.STRtree(self.areas) if self.areas else None

    def uncovered(self, line: LineString) -> list[LineString]:
        """Partes de ``line`` fora das áreas."""
        if self.tree is None:
            return [line]
        near = self.tree.query(line, predicate="intersects")
        if not len(near):
            return [line]
        local = shapely.union_all([self.areas[i] for i in near])
        return _lines(line.difference(local))


def _stable_id(line: LineString, tags: dict[str, str]) -> str:
    c = np.round(np.asarray(line.coords), 0)
    if tuple(c[0]) > tuple(c[-1]):
        c = c[::-1]
    raw = json.dumps([c.tolist(), sorted(tags.items())]).encode()
    return hashlib.sha1(raw).hexdigest()[:12]


def _merge(lines: list[tuple[LineString, list[str]]]) -> list[tuple[LineString, list[str]]]:
    """Junta troços que se tocam (até à próxima junção), com as fontes de cada um."""
    if not lines:
        return []
    geoms = [g for g, _ in lines]
    joined = shapely.snap(shapely.union_all(geoms), shapely.union_all(geoms), JOIN_M)
    merged = _lines(shapely.line_merge(shapely.union_all([joined])), 0.5)
    tree = shapely.STRtree(geoms)
    out = []
    for m in merged:
        near = tree.query(m.buffer(0.1), predicate="intersects")
        srcs = sorted({s for i in near for s in lines[i][1]})
        out.append((m, srcs))
    return out


# ---------------------------------------------------------- ligação ao landuse


def _ring_edge(ring: LineString, a: Point, b: Point) -> LineString:
    """Troço do anel entre as projeções de ``a`` e ``b`` (pelo caminho mais curto)."""
    da, db = ring.project(a), ring.project(b)
    total = ring.length
    if abs(db - da) <= total / 2:
        return substring(ring, da, db)
    # Pelo outro lado (passa pelo início do anel).
    if da > db:
        first, second = substring(ring, da, total), substring(ring, 0, db)
    else:
        first, second = substring(ring, da, 0), substring(ring, total, db)
    coords = [*first.coords, *list(second.coords)[1:]]
    return LineString(coords)


def landuse_runs(
    line: LineString, rings: list[OsmLanduseRing], tree: shapely.STRtree
) -> list[LanduseRun]:
    """Troços de ``line`` que correm ao longo de limites de landuse (paralelos, perto)."""
    n = max(2, int(math.ceil(line.length / SAMPLE_M)) + 1)
    dists = np.linspace(0, line.length, n)
    eps = min(0.5, line.length / 4)
    pts = shapely.line_interpolate_point(line, dists)
    p0 = shapely.get_coordinates(shapely.line_interpolate_point(line, np.maximum(0, dists - eps)))
    p1 = shapely.get_coordinates(
        shapely.line_interpolate_point(line, np.minimum(line.length, dists + eps))
    )
    a1 = np.arctan2(p1[:, 1] - p0[:, 1], p1[:, 0] - p0[:, 0])
    out: list[LanduseRun] = []
    for i in tree.query(line, predicate="dwithin", distance=SNAP_M):
        r = rings[i]
        ring = LineString(r.polygon.exterior.coords)
        dist = shapely.distance(ring, pts)
        s = shapely.line_locate_point(ring, pts)
        q0 = shapely.get_coordinates(shapely.line_interpolate_point(ring, np.maximum(0, s - eps)))
        q1 = shapely.get_coordinates(
            shapely.line_interpolate_point(ring, np.minimum(ring.length, s + eps))
        )
        a2 = np.arctan2(q1[:, 1] - q0[:, 1], q1[:, 0] - q0[:, 0])
        diff = np.abs((a1 - a2 + np.pi / 2) % np.pi - np.pi / 2)
        ok = (dist <= SNAP_M) & (np.degrees(diff) <= SNAP_ANGLE_DEG)
        k = 0
        while k < n:
            if not ok[k]:
                k += 1
                continue
            j = k
            while j + 1 < n and ok[j + 1]:
                j += 1
            s0, s1 = float(dists[k]), float(dists[j])
            if s1 - s0 >= MIN_RUN_M:
                edge = _ring_edge(ring, line.interpolate(s0), line.interpolate(s1))
                offset = float(dist[k : j + 1].max())
                out.append(LanduseRun(r.way_id, r.label, s0, s1, offset, edge))
            k = j + 1
    return out


# --------------------------------------------------------------- propostas


PROPOSED_BARRIERS = {"wall"}  # só muros (decisão do utilizador: sem vedações)


def as_wall(tags: dict[str, str]) -> dict[str, str]:
    """``barrier=retaining_wall`` passa a ``barrier=wall`` (decisão do utilizador)."""
    if tags.get("barrier") == "retaining_wall":
        return {**tags, "barrier": "wall"}
    return tags


def propose_walls(barriers: list[MunicipalBarrier], ctx: WallContext) -> list[WallProposal]:
    """Propostas de muros da área, com os troços que coincidem com limites de landuse."""
    # 1. Por prioridade: o mesmo muro noutra camada (já coberto) sai.
    kept: list[tuple[LineString, dict[str, str], str, str]] = []
    higher: list[BaseGeometry] = []
    for prio in sorted({b.priority for b in barriers}):
        same = [b for b in barriers if b.priority == prio]
        cover = _Cover(higher)
        for b in same:
            tags = as_wall(b.tags)
            if tags.get("barrier") not in PROPOSED_BARRIERS:
                continue  # vedações: não se propõem (mas cobrem o mesmo troço noutras camadas)
            for piece in cover.uncovered(b.geometry):
                kept.append((piece, tags, b.layer, b.key))
        higher += list(shapely.buffer(np.array([b.geometry for b in same]), DEDUP_M))
    # 2. Já mapeado no OSM (muros) ou dentro de edifícios do OSM.
    osm_areas = list(shapely.buffer(np.array(ctx.barriers), DEDUP_M)) if ctx.barriers else []
    if ctx.buildings:
        osm_areas += list(shapely.buffer(np.array(ctx.buildings), -0.2))
    osm_cover = _Cover(osm_areas)
    pieces: dict[tuple[tuple[str, str], ...], list[tuple[LineString, list[str]]]] = {}
    layers: dict[tuple[tuple[str, str], ...], str] = {}
    for line, tags, layer, key in kept:
        group = tuple(sorted(tags.items()))
        for piece in osm_cover.uncovered(line):
            pieces.setdefault(group, []).append((piece, [key]))
            layers.setdefault(group, layer)
    # 3. Linhas contínuas, por tipo.
    tree = shapely.STRtree([r.polygon.exterior for r in ctx.landuse])
    out: list[WallProposal] = []
    for group, items in pieces.items():
        tags = dict(group)
        for line, srcs in _merge(items):
            if line.length < MIN_LEN_M:
                continue
            runs = landuse_runs(line, ctx.landuse, tree) if ctx.landuse else []
            layer = layers[group]
            out.append(WallProposal(_stable_id(line, tags), tags, line, layer, srcs, runs))
    # Ids únicos (dois troços iguais arredondados): sufixo.
    seen: dict[str, int] = {}
    for w in out:
        if w.id in seen:
            seen[w.id] += 1
            w.id = f"{w.id}-{seen[w.id]}"
        else:
            seen[w.id] = 0
    return sorted(out, key=lambda w: w.id)


# ------------------------------------------------------------ exportação


def with_run_vertices(w: WallProposal) -> LineString:
    """A linha do muro com vértices nas pontas de cada troço ao longo do landuse."""
    cuts = sorted({round(d, 3) for r in w.runs for d in (r.start_m, r.end_m)} - {0.0})
    line = w.geometry
    if not cuts:
        return line
    pieces = []
    last = 0.0
    for d in [*cuts, line.length]:
        if d - last > 1e-6:
            pieces.append(substring(line, last, d))
        last = d
    coords = list(pieces[0].coords)
    for p in pieces[1:]:
        coords += list(p.coords)[1:]
    return LineString(coords)


def run_faces(
    w: WallProposal, ring_polygon: Polygon, run: LanduseRun
) -> list[tuple[Polygon, bool]]:
    """Faixas entre o limite atual do landuse e o muro: (faixa, o landuse passa a incluí-la).

    Com o landuse ajustado (``∪`` das que estão fora, ``−`` das que estão dentro), o limite
    fica exatamente sobre o muro nesse troço.
    """
    wall = substring(w.geometry, run.start_m, run.end_m)
    ring = LineString(ring_polygon.exterior.coords)
    edge = _ring_edge(ring, Point(wall.coords[0]), Point(wall.coords[-1]))
    shell = [*wall.coords, *reversed(edge.coords)]
    if len(shell) < 3:
        return []
    faces = [
        p
        for p in getattr(shapely.make_valid(Polygon(shell)), "geoms", [None])
        if isinstance(p, Polygon)
    ]
    single = shapely.make_valid(Polygon(shell))
    if isinstance(single, Polygon):
        faces = [single]
    out = []
    for f in faces:
        if f.area < 1e-4:
            continue
        inside = ring_polygon.contains(f.representative_point())
        out.append((f, not inside))
    return out
