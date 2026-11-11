"""Topologia das partes de um edifício (``building:part``) com vértices partilhados.

Regras (como no OSM e no editor iD):

- **vértices partilhados:** partes vizinhas usam o mesmo vértice nos pontos comuns e,
  na exportação, o mesmo nó;
- **sem junções em T:** um vértice que fica sobre o lado de outra parte é inserido
  nesse lado, para as partes ficarem ligadas;
- **vértices sobre o contorno:**
  - *outline*: coincide com um nó do contorno do edifício;
  - *edge*: fica sobre um lado do contorno, e na exportação é inserido nesse lado;
  - *free*: interior;
- **partição:** as partes cobrem o contorno sem se sobreporem; desvios são reportados
  por ``validate_layout``.

Coordenadas no referencial do OSM (CRS de processamento), em metros.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Literal

import numpy as np
import shapely
from shapely.geometry import LineString, MultiPolygon, Polygon
from shapely.geometry.base import BaseGeometry
from shapely.geometry.polygon import orient
from shapely.ops import split as shapely_split

from osm3d.domain.models import PartsLayout

SNAP_M = 0.01  # vértices a menos de 1 cm são o mesmo vértice
MIN_PART_AREA_M2 = 3.0  # partes menores são juntadas à vizinha (lascas de recortes)
MIN_PART_FRACTION = 0.03

VertexKind = Literal["outline", "edge", "free"]


@dataclass(frozen=True)
class VertexInfo:
    kind: VertexKind
    outline_index: int | None = None  # "outline": índice do nó; "edge": índice do lado
    t: float | None = None  # "edge": posição no lado (0–1)


# --------------------------------------------------------------- construção


def _polygons(geom: BaseGeometry) -> list[Polygon]:
    if isinstance(geom, Polygon):
        return [geom] if not geom.is_empty else []
    if isinstance(geom, MultiPolygon):
        return [g for g in geom.geoms if not g.is_empty]
    return [g for g in getattr(geom, "geoms", []) if isinstance(g, Polygon) and not g.is_empty]


def on_segment(p: np.ndarray, a: np.ndarray, b: np.ndarray, tol: float) -> float | None:
    """Parâmetro t ∈ (0, 1) se ``p`` está no interior do segmento ab (a ``tol``)."""
    d = b - a
    length2 = float(d @ d)
    if length2 == 0:
        return None
    t = float((p - a) @ d) / length2
    if t <= 1e-6 or t >= 1 - 1e-6:
        return None
    return t if float(np.linalg.norm(a + t * d - p)) <= tol else None


def layout_from_polygons(
    polygons: list[Polygon], source: Literal["suggested", "manual"] = "manual"
) -> PartsLayout:
    """Partes com vértices partilhados (fusão a 1 cm) e sem junções em T."""
    vertices: list[np.ndarray] = []
    rings: list[list[int]] = []

    def vertex_id(p: np.ndarray) -> int:
        for i, v in enumerate(vertices):
            if float(np.linalg.norm(v - p)) <= SNAP_M:
                return i
        vertices.append(p)
        return len(vertices) - 1

    for poly in polygons:
        poly = orient(poly, sign=1.0)
        ring: list[int] = []
        for xy in np.asarray(poly.exterior.coords)[:-1]:
            k = vertex_id(np.asarray(xy, dtype=float))
            if not ring or ring[-1] != k:
                ring.append(k)
        if len(ring) > 1 and ring[0] == ring[-1]:
            ring.pop()
        rings.append(ring)

    # Junções em T: inserir em cada lado os vértices que estão sobre ele.
    for ring in rings:
        i = 0
        while i < len(ring):
            a, b = vertices[ring[i]], vertices[ring[(i + 1) % len(ring)]]
            on = [
                (t, k)
                for k, p in enumerate(vertices)
                if k not in (ring[i], ring[(i + 1) % len(ring)])
                and (t := on_segment(p, a, b, SNAP_M)) is not None
            ]
            if on:
                on.sort()
                ring[i + 1 : i + 1] = [k for _, k in on]
                i += len(on)
            i += 1

    rings = [_drop_collinear_duplicates(r) for r in rings]
    used = sorted({k for r in rings for k in r})
    remap = {old: new for new, old in enumerate(used)}
    return PartsLayout(
        vertices=[(float(vertices[k][0]), float(vertices[k][1])) for k in used],
        parts=[[remap[k] for k in r] for r in rings if len(r) >= 3],
        source=source,
    )


def _drop_collinear_duplicates(ring: list[int]) -> list[int]:
    """Remove vértices repetidos e "agulhas" (… a, b, a …) de largura nula.

    As agulhas aparecem quando um corte é quase colinear com um lado do contorno: a
    lasca entre os dois colapsa ao fundir vértices a 1 cm e deixa uma ida e volta.
    """
    out = list(ring)
    changed = True
    while changed and len(out) >= 3:
        changed = False
        dedup: list[int] = []
        for k in out:
            if not dedup or dedup[-1] != k:
                dedup.append(k)
        while len(dedup) > 1 and dedup[0] == dedup[-1]:
            dedup.pop()
        if len(dedup) != len(out):
            changed = True
        out = dedup
        n = len(out)
        for i in range(n):
            if n >= 3 and out[(i - 1) % n] == out[(i + 1) % n]:
                # a, b, a → a  (remove b e o a repetido)
                del out[i]
                del out[(i - 1) % (n - 1) if i > 0 else -1]
                changed = True
                break
    return out


def layout_polygons(layout: PartsLayout) -> list[Polygon]:
    return [layout.polygon(i) for i in range(len(layout.parts))]


# ------------------------------------------------------------- operações


def _absorb_slivers(pieces: list[Polygon], total_area: float) -> list[Polygon]:
    """Junta lascas (recortes minúsculos) à peça vizinha com maior fronteira comum."""
    pieces = sorted(pieces, key=lambda p: p.area, reverse=True)
    keep = [p for p in pieces if p.area >= max(MIN_PART_AREA_M2, MIN_PART_FRACTION * total_area)]
    for small in (p for p in pieces if p not in keep):
        if not keep:
            keep.append(small)
            continue
        shared = [k.boundary.intersection(small.boundary).length for k in keep]
        j = int(np.argmax(shared))
        merged = keep[j].union(small)
        keep[j] = (
            merged if isinstance(merged, Polygon) else max(_polygons(merged), key=lambda g: g.area)
        )
    return keep


def suggest_layout(footprint: BaseGeometry, main_region: BaseGeometry) -> PartsLayout | None:
    """Volume principal + resto do contorno (volumes secundários) como partes."""
    main = _polygons(footprint.intersection(main_region))
    rest = _polygons(footprint.difference(main_region))
    if not main or not rest:
        return None
    pieces = _absorb_slivers([max(main, key=lambda g: g.area), *rest], footprint.area)
    if len(pieces) < 2:
        return None
    return layout_from_polygons(pieces, source="suggested")


def split_part(
    layout: PartsLayout, index: int, a: tuple[float, float], b: tuple[float, float]
) -> PartsLayout:
    """Divide a parte ``index`` pela reta que passa em ``a`` e ``b``."""
    poly = layout.polygon(index)
    pa, pb = np.asarray(a, float), np.asarray(b, float)
    d = pb - pa
    if float(np.linalg.norm(d)) < 0.1:
        raise ValueError("Os dois pontos da divisão estão demasiado próximos")
    # Prolonga a reta para lá da parte (o corte vai de lado a lado).
    reach = math.hypot(*np.subtract(poly.bounds[2:], poly.bounds[:2])) + 1.0
    u = d / np.linalg.norm(d)
    line = LineString([pa - u * reach, pb + u * reach])
    pieces = _polygons(shapely_split(poly, line))
    if len(pieces) < 2:
        raise ValueError("A linha não atravessa a parte")
    others = [p for i, p in enumerate(layout_polygons(layout)) if i != index]
    new = _absorb_slivers(pieces, poly.area)
    return layout_from_polygons([*others[:index], *new, *others[index:]], source="manual")


def merge_parts(layout: PartsLayout, index: int) -> PartsLayout:
    """Junta a parte ``index`` à vizinha com quem partilha a maior fronteira."""
    polys = layout_polygons(layout)
    if len(polys) < 2:
        raise ValueError("Não há partes para juntar")
    target = polys[index]
    shared = [
        (-1.0 if i == index else target.boundary.intersection(p.boundary).length, i)
        for i, p in enumerate(polys)
    ]
    length, j = max(shared)
    if length <= 0:
        raise ValueError("A parte não toca em nenhuma outra")
    merged = _polygons(shapely.union_all([target, polys[j]]).buffer(0))
    if len(merged) != 1:
        raise ValueError("A junção não dá um polígono único")
    out = [p for i, p in enumerate(polys) if i not in (index, j)]
    out.insert(min(index, j), merged[0])
    return layout_from_polygons(out, source="manual")


# --------------------------------------------------------- contorno / validação


def vertex_info(layout: PartsLayout, outline: Polygon) -> list[VertexInfo]:
    """Relação de cada vértice com o contorno (nó, lado ou interior)."""
    coords = np.asarray(orient(outline, 1.0).exterior.coords)[:-1]
    n = len(coords)
    out: list[VertexInfo] = []
    for v in layout.vertices:
        p = np.asarray(v, float)
        dist = np.linalg.norm(coords - p, axis=1)
        if float(dist.min()) <= SNAP_M:
            out.append(VertexInfo("outline", int(dist.argmin())))
            continue
        info = VertexInfo("free")
        for i in range(n):
            t = on_segment(p, coords[i], coords[(i + 1) % n], SNAP_M)
            if t is not None:
                info = VertexInfo("edge", i, t)
                break
        out.append(info)
    return out


def validate_layout(layout: PartsLayout, outline: BaseGeometry) -> list[str]:
    """Problemas da divisão (vazio = válida)."""
    problems: list[str] = []
    polys = layout_polygons(layout)
    if len(polys) < 2:
        problems.append("Uma divisão precisa de pelo menos 2 partes")
    for i, p in enumerate(polys):
        if not p.is_valid:
            problems.append(f"Parte {i + 1}: polígono inválido (lados cruzados?)")
        elif p.area < 1.0:
            problems.append(f"Parte {i + 1}: área quase nula")
    valid = [p for p in polys if p.is_valid]
    for i in range(len(valid)):
        for j in range(i + 1, len(valid)):
            overlap = valid[i].intersection(valid[j]).area
            if overlap > 0.05:
                problems.append(f"Partes {i + 1} e {j + 1} sobrepõem-se ({overlap:.1f} m²)")
    if valid:
        union = shapely.union_all(valid)
        outside = union.difference(outline.buffer(SNAP_M)).area
        uncovered = outline.difference(union.buffer(SNAP_M)).area
        if outside > 0.05:
            problems.append(f"{outside:.1f} m² de partes fora do contorno do edifício")
        if uncovered > 0.5:
            problems.append(f"{uncovered:.1f} m² do edifício sem parte")
    return problems
