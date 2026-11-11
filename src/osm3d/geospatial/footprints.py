"""Leitura, reprojeção (WGS84 → CRS de processamento) e limpeza de footprints OSM."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from xml.etree import ElementTree as ET

import shapely
from pyproj import Transformer
from shapely.geometry import LineString, MultiPolygon, Polygon
from shapely.geometry.base import BaseGeometry
from shapely.ops import polygonize, unary_union

from osm3d.domain.models import Building

log = logging.getLogger(__name__)


@dataclass
class _Way:
    id: int
    version: int
    nodes: list[int]
    tags: dict[str, str]


@dataclass
class _Relation:
    id: int
    version: int
    members: list[tuple[str, int, str]]  # (tipo, ref, role)
    tags: dict[str, str] = field(default_factory=dict)


@dataclass
class OsmData:
    nodes: dict[int, tuple[float, float]]  # id → (lon, lat)
    ways: dict[int, _Way]
    relations: list[_Relation]


def parse_osm_xml(path: Path) -> OsmData:
    root = ET.parse(path).getroot()
    nodes: dict[int, tuple[float, float]] = {}
    ways: dict[int, _Way] = {}
    relations: list[_Relation] = []
    for el in root:
        tags = {t.attrib["k"]: t.attrib["v"] for t in el.findall("tag")}
        if el.tag == "node":
            nodes[int(el.attrib["id"])] = (float(el.attrib["lon"]), float(el.attrib["lat"]))
        elif el.tag == "way":
            ways[int(el.attrib["id"])] = _Way(
                id=int(el.attrib["id"]),
                version=int(el.attrib.get("version", 0)),
                nodes=[int(nd.attrib["ref"]) for nd in el.findall("nd")],
                tags=tags,
            )
        elif el.tag == "relation":
            relations.append(
                _Relation(
                    id=int(el.attrib["id"]),
                    version=int(el.attrib.get("version", 0)),
                    members=[
                        (m.attrib["type"], int(m.attrib["ref"]), m.attrib.get("role", ""))
                        for m in el.findall("member")
                    ],
                    tags=tags,
                )
            )
    return OsmData(nodes, ways, relations)


def _polygonal(geom: BaseGeometry) -> Polygon | MultiPolygon | None:
    """Garante geometria válida e só com partes poligonais."""
    if not geom.is_valid:
        geom = shapely.make_valid(geom)
    if isinstance(geom, Polygon | MultiPolygon):
        return None if geom.is_empty else geom
    polys = [g for g in getattr(geom, "geoms", []) if isinstance(g, Polygon | MultiPolygon)]
    if not polys:
        return None
    merged = unary_union(polys)
    return merged if isinstance(merged, Polygon | MultiPolygon) else None


def _way_coords(way: _Way, data: OsmData) -> list[tuple[float, float]] | None:
    try:
        return [data.nodes[n] for n in way.nodes]
    except KeyError:
        return None  # nós fora da extração


def _way_polygon(way: _Way, data: OsmData) -> Polygon | None:
    if len(way.nodes) < 4 or way.nodes[0] != way.nodes[-1]:
        return None
    coords = _way_coords(way, data)
    return Polygon(coords) if coords else None


def _relation_polygon(rel: _Relation, data: OsmData) -> BaseGeometry | None:
    rings: dict[str, list[LineString]] = {"outer": [], "inner": []}
    for mtype, ref, role in rel.members:
        if mtype != "way" or ref not in data.ways:
            continue
        coords = _way_coords(data.ways[ref], data)
        if coords and len(coords) >= 2:
            rings["inner" if role == "inner" else "outer"].append(LineString(coords))
    if not rings["outer"]:
        return None
    outer = unary_union(list(polygonize(rings["outer"])))
    if rings["inner"]:
        outer = outer.difference(unary_union(list(polygonize(rings["inner"]))))
    return outer


def load_footprints(osm_file: Path, crs: str, min_area_m2: float) -> list[Building]:
    """Constrói ``Building`` (sem partes) a partir de um ficheiro ``.osm``.

    Inclui ways ``building=*`` fechadas e relações multipolígono ``building=*``.
    ``building:part`` não são edifícios (ficam para a Fase 4/6). Corrige geometrias
    inválidas, descarta áreas inferiores a ``min_area_m2`` e preserva
    ``id``/``version``/tags originais.
    """
    data = parse_osm_xml(osm_file)
    to_crs = Transformer.from_crs("EPSG:4326", crs, always_xy=True)

    candidates: list[tuple[str, int, int, dict[str, str], BaseGeometry | None]] = []
    for way in data.ways.values():
        if "building" in way.tags:
            candidates.append(("way", way.id, way.version, way.tags, _way_polygon(way, data)))
    for rel in data.relations:
        if "building" in rel.tags and rel.tags.get("type") == "multipolygon":
            candidates.append(
                ("relation", rel.id, rel.version, rel.tags, _relation_polygon(rel, data))
            )

    buildings: list[Building] = []
    skipped = 0
    for osm_type, osm_id, version, tags, geom_wgs84 in candidates:
        if geom_wgs84 is None or geom_wgs84.is_empty:
            skipped += 1
            continue
        projected = shapely.transform(geom_wgs84, lambda xy: _project(to_crs, xy))
        footprint = _polygonal(projected)
        if footprint is None or footprint.area < min_area_m2:
            skipped += 1
            continue
        buildings.append(
            Building(
                osm_type=osm_type,  # type: ignore[arg-type]
                osm_id=osm_id,
                osm_version=version,
                footprint=footprint,
                existing_tags=tags,
            )
        )
    log.info("%d edifícios carregados, %d ignorados", len(buildings), skipped)
    return _attach_osm_parts(buildings, data, to_crs)


def _attach_osm_parts(
    buildings: list[Building], data: OsmData, to_crs: Transformer
) -> list[Building]:
    """Associa a cada edifício as ``building:part`` já mapeadas dentro do contorno."""
    parts: list[tuple[int, BaseGeometry]] = []
    for way in data.ways.values():
        if "building:part" in way.tags and (poly := _way_polygon(way, data)) is not None:
            projected = _polygonal(shapely.transform(poly, lambda xy: _project(to_crs, xy)))
            if projected is not None:
                parts.append((way.id, projected.representative_point()))
    if not parts or not buildings:
        return buildings
    tree = shapely.STRtree([b.footprint.buffer(0.5) for b in buildings])
    part_idx, bld_idx = tree.query([p for _, p in parts], predicate="within")
    found: dict[int, list[int]] = {}
    for pi, bi in zip(part_idx.tolist(), bld_idx.tolist(), strict=True):
        found.setdefault(bi, []).append(parts[pi][0])
    return [
        b.model_copy(update={"osm_parts": sorted(found[i])}) if i in found else b
        for i, b in enumerate(buildings)
    ]


def _project(transformer: Transformer, xy):  # type: ignore[no-untyped-def]
    x, y = transformer.transform(xy[:, 0], xy[:, 1])
    xy = xy.copy()
    xy[:, 0], xy[:, 1] = x, y
    return xy
