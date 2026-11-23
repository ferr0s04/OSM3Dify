from __future__ import annotations

import json
from pathlib import Path
from xml.etree import ElementTree as ET

from pyproj import Transformer
from shapely.geometry import LineString, Polygon, box

from osm3d.domain.models import Building, PartsLayout
from osm3d.export.osm_edits import plan_edits
from osm3d.parts import layout_from_polygons
from osm3d.validation.topology import (
    ADJUSTS_OSM,
    CROSSES_HIGHWAY,
    OVERLAPS_NEW,
    OVERLAPS_OSM,
    STRADDLES_LANDUSE,
    TRIMMED,
    Feature,
    Surroundings,
    TopologyRules,
    load_surroundings,
    resolve_topology,
)

RULES = TopologyRules(clearance_m=0.1, max_trim_m=1.0, max_trim_fraction=0.15)
CRS = "EPSG:3763"
TO_WGS = Transformer.from_crs(CRS, "EPSG:4326", always_xy=True)


def _new(fp: Polygon, osm_id: int = 1, *, municipal: bool = True, **update: object) -> Building:
    return Building(
        osm_type="new",
        osm_id=osm_id,
        osm_version=0,
        footprint=fp,
        existing_tags={"building": "yes"},
        diagnostics={"municipal_gid": 1.0} if municipal else {},
        **update,
    )


def _env(
    buildings: tuple[Polygon, ...] = (),
    highways: tuple[LineString, ...] = (),
    landuse: tuple[Polygon, ...] = (),
) -> Surroundings:
    env = Surroundings()
    env.buildings.features += [
        Feature(g, f"way/{100 + i} (building=yes)", "way", 100 + i) for i, g in enumerate(buildings)
    ]
    env.highways.features += [
        Feature(g, f"highway=footway (way/{200 + i})", "way", 200 + i)
        for i, g in enumerate(highways)
    ]
    env.landuse.features += [
        Feature(g, f"landuse=residential (way/{300 + i})", "way", 300 + i)
        for i, g in enumerate(landuse)
    ]
    return env


def _codes(b: Building) -> set[str]:
    return {f.code for f in b.qa_flags}


LAND = box(-50, 0, 50, 50)


# ------------------------------------------------------------- modo "trim"


def test_trim_mode_cuts_landuse_sliver_inside_with_clearance() -> None:
    lidar = _new(box(0, -0.5, 10, 9.5), municipal=False)  # LiDAR: recorta-se por omissão
    (b,), res = resolve_topology([lidar], _env(landuse=(LAND,)), RULES)
    assert TRIMMED in _codes(b) and res.trimmed == 1 and not res.adjustments
    assert b.footprint.within(LAND)
    assert abs(b.footprint.bounds[1] - 0.1) < 1e-6  # folga de 0,1 m ao limite
    assert b.osm_id == 1  # o id (e a revisão) não muda


def test_gap_to_landuse_is_never_touched() -> None:
    # Edifício 3 cm dentro do limite (logradouro/muro): o afastamento pode ser verdadeiro.
    for municipal in (True, False):
        fp = box(0, 0.03, 10, 10)
        (b,), res = resolve_topology([_new(fp, municipal=municipal)], _env(landuse=(LAND,)), RULES)
        assert b.footprint.equals(fp) and not _codes(b) and not res.adjustments


def test_large_landuse_straddle_is_only_flagged() -> None:
    fp = box(0, -4, 10, 6)  # 40 % fora: não é uma lasca
    for municipal in (True, False):
        (b,), res = resolve_topology([_new(fp, municipal=municipal)], _env(landuse=(LAND,)), RULES)
        assert b.footprint.equals(fp) and not res.adjustments
        assert STRADDLES_LANDUSE in _codes(b) and res.conflicts == 0  # aviso, não conflito


def test_landuse_can_be_disabled() -> None:
    fp = box(0, -0.5, 10, 9.5)
    rules = TopologyRules(landuse=False)
    (b,), res = resolve_topology([_new(fp, municipal=False)], _env(landuse=(LAND,)), rules)
    assert b.footprint.equals(fp) and not _codes(b)


def test_trim_mode_osm_building_overlap_trimmed_or_flagged() -> None:
    osm = box(10, 0, 20, 10)
    small = _new(box(0, 0, 10.4, 10), 1, municipal=False)  # 0,4 m por cima do vizinho
    big = _new(box(0, 20, 14, 30), 2, municipal=False)
    osm2 = box(10, 20, 20, 30)  # 4 m por cima: duplicado/contorno errado
    out, res = resolve_topology([small, big], _env(buildings=(osm, osm2)), RULES)
    assert out[0].footprint.distance(osm) >= 0.09 and TRIMMED in _codes(out[0])
    assert out[1].footprint.equals(big.footprint) and OVERLAPS_OSM in _codes(out[1])
    assert res.conflicts == 1 and out[1].needs_review


def test_trim_mode_highway_crossing_a_corner_is_cut() -> None:
    corner = LineString([(9.4, -5), (9.4, 20)])  # passa 0,6 m dentro do lado este
    (b,), _ = resolve_topology(
        [_new(box(0, 0, 10, 10), municipal=False)], _env(highways=(corner,)), RULES
    )
    assert not b.footprint.intersects(corner) and b.footprint.area > 85


def test_highway_through_or_ending_inside_is_flagged() -> None:
    fp = box(0, 0, 10, 10)
    through = LineString([(5, -5), (5, 20)])
    dead_end = LineString([(9.5, -5), (9.5, 5)])  # acaba dentro: o recorte seria um entalhe
    for line in (through, dead_end):
        (b,), res = resolve_topology([_new(fp, municipal=False)], _env(highways=(line,)), RULES)
        assert b.footprint.equals(fp) and CROSSES_HIGHWAY in _codes(b) and res.conflicts == 1


def test_highway_touching_a_wall_is_not_a_crossing() -> None:
    along = LineString([(-5, 0), (15, 0)])  # ao longo da fachada
    (b,), res = resolve_topology([_new(box(0, 0, 10, 10))], _env(highways=(along,)), RULES)
    assert not _codes(b) and res.conflicts == 0


# -------------------------------------------------------------- modo "osm"


def test_osm_mode_keeps_municipal_outline_and_plans_landuse_adjustment() -> None:
    fp = box(0, -0.5, 10, 9.5)
    (b,), res = resolve_topology([_new(fp)], _env(landuse=(LAND,)), RULES)
    assert b.footprint.equals(fp)  # o contorno municipal manda
    assert ADJUSTS_OSM in _codes(b) and res.adjusting == 1
    (adj,) = res.adjustments
    assert adj.include and adj.feature.osm_id == 300  # o landuse passa a contornar o edifício


def test_osm_mode_highway_is_flagged_not_trimmed() -> None:
    fp = box(0, 0, 10, 10)
    corner = LineString([(9.4, -5), (9.4, 20)])
    (b,), res = resolve_topology([_new(fp)], _env(highways=(corner,)), RULES)
    assert b.footprint.equals(fp) and CROSSES_HIGHWAY in _codes(b)


def test_osm_mode_existing_building_loses_the_sliver() -> None:
    fp = box(0, 0, 10.4, 10)
    (b,), res = resolve_topology([_new(fp)], _env(buildings=(box(10, 0, 20, 10),)), RULES)
    assert b.footprint.equals(fp)
    (adj,) = res.adjustments
    assert not adj.include and adj.feature.osm_id == 100


def test_keep_mode_changes_nothing() -> None:
    fp = box(0, -0.5, 10, 9.5)
    b0 = _new(fp, municipal=False, topology_mode="keep")
    (b,), res = resolve_topology([b0], _env(landuse=(LAND,)), RULES)
    assert b.footprint.equals(fp) and not res.adjustments and STRADDLES_LANDUSE in _codes(b)


def test_relation_landuse_is_not_adjusted() -> None:
    env = Surroundings()
    env.landuse.features.append(Feature(LAND, "landuse=residential (relation/9)", "relation", 9))
    (b,), res = resolve_topology([_new(box(0, -0.5, 10, 9.5))], env, RULES)
    assert not res.adjustments and STRADDLES_LANDUSE in _codes(b)


# --------------------------------------------------------- entre edifícios novos


def test_overlapping_new_buildings_end_up_touching() -> None:
    lidar = _new(box(9.6, 0, 20, 10), 1, municipal=False)
    muni = _new(box(0, 0, 10, 10), 2)
    out, _ = resolve_topology([lidar, muni], _env(), RULES)
    by_id = {b.osm_id: b for b in out}
    assert by_id[2].footprint.equals(muni.footprint)  # o municipal fica como estava
    assert by_id[1].footprint.intersection(by_id[2].footprint).area < 1e-6
    assert by_id[1].footprint.distance(by_id[2].footprint) < 1e-6  # encostados, sem folga


def test_heavily_overlapping_new_buildings_are_flagged() -> None:
    a, b = _new(box(0, 0, 10, 10), 1), _new(box(5, 0, 15, 10), 2)
    out, res = resolve_topology([a, b], _env(), RULES)
    assert all(OVERLAPS_NEW in _codes(x) for x in out) and res.conflicts == 2


def test_existing_osm_buildings_untouched_and_second_pass_is_stable() -> None:
    osm_way = Building(osm_type="way", osm_id=5, osm_version=1, footprint=box(0, -1, 10, 9))
    new = _new(box(20, -0.5, 30, 9.5), municipal=False)
    env = _env(landuse=(LAND,))
    out, _ = resolve_topology([osm_way, new], env, RULES)
    assert out[0] is osm_way
    again, res = resolve_topology(out, env, RULES)
    assert again[1].footprint.equals(out[1].footprint) and res.trimmed == 0
    assert TRIMMED in _codes(again[1])  # a nota do recorte anterior fica


def test_parts_layout_is_clipped_with_the_outline() -> None:
    fp = box(0, -0.5, 12, 9.5)
    layout = layout_from_polygons([box(0, -0.5, 8, 9.5), box(8, -0.5, 12, 9.5)])
    (b,), _ = resolve_topology(
        [_new(fp, municipal=False, layout=layout)], _env(landuse=(LAND,)), RULES
    )
    assert TRIMMED in _codes(b)
    assert isinstance(b.layout, PartsLayout) and len(b.layout.parts) == 2
    assert all(b.layout.polygon(i).within(b.footprint.buffer(1e-6)) for i in range(2))


def test_load_surroundings_reads_context(tmp_path: Path) -> None:
    def geom(coords: list[tuple[float, float]]) -> list[dict[str, float]]:
        return [dict(zip(("lon", "lat"), TO_WGS.transform(x, y), strict=True)) for x, y in coords]

    sq = [(0, 0), (10, 0), (10, 10), (0, 10), (0, 0)]
    line = geom(sq[:2])
    elements = [
        {"type": "way", "id": 1, "tags": {"building": "house"}, "geometry": geom(sq)},
        {"type": "way", "id": 2, "tags": {"landuse": "farmland"}, "geometry": geom(sq)},
        {"type": "way", "id": 3, "tags": {"highway": "footway"}, "geometry": line},
        {"type": "way", "id": 4, "tags": {"highway": "service", "tunnel": "yes"}, "geometry": line},
        {"type": "way", "id": 5, "tags": {"highway": "footway", "layer": "1"}, "geometry": line},
    ]
    path = tmp_path / "ctx.json"
    path.write_text(json.dumps({"elements": elements}), encoding="utf-8")
    env = load_surroundings(path, CRS)
    assert [f.label for f in env.buildings.features] == ["way/1 (building=house)"]
    assert [f.label for f in env.landuse.features] == ["landuse=farmland (way/2)"]
    assert [f.label for f in env.highways.features] == ["highway=footway (way/3)"]
    assert env.landuse.features[0].editable
    assert abs(env.buildings.features[0].geom.area - 100) < 0.5


# ------------------------------------------------------- ajustes ao OSM (export)


def _osm_xml(
    nodes: dict[int, tuple[float, float]], ways: dict[int, tuple[list[int], dict]]
) -> bytes:
    root = ET.Element("osm", version="0.6")
    for nid, (x, y) in nodes.items():
        lon, lat = TO_WGS.transform(x, y)
        ET.SubElement(root, "node", id=str(nid), version="1", lat=f"{lat:.9f}", lon=f"{lon:.9f}")
    for wid, (refs, tags) in ways.items():
        w = ET.SubElement(root, "way", id=str(wid), version="3")
        for r in refs:
            ET.SubElement(w, "nd", ref=str(r))
        for k, v in tags.items():
            ET.SubElement(w, "tag", k=k, v=v)
    return ET.tostring(root)


def test_glued_landuses_are_adjusted_together_and_stay_glued() -> None:
    # Residencial (y ≥ 0) e agrícola (y ≤ 0) colados no limite y = 0 (nós 2 e 3 partilhados);
    # o edifício municipal passa 0,5 m o limite para dentro do agrícola.
    nodes = {1: (-50, 50), 2: (-50, 0), 3: (50, 0), 4: (50, 50), 5: (-50, -50), 6: (50, -50)}
    ways = {
        300: ([1, 2, 3, 4, 1], {"landuse": "residential"}),
        301: ([2, 5, 6, 3, 2], {"landuse": "farmland"}),
    }
    xml = _osm_xml(nodes, ways)
    env = Surroundings()
    env.landuse.features += [
        Feature(
            Polygon([nodes[i] for i in refs]), f"landuse={t['landuse']} (way/{wid})", "way", wid
        )
        for wid, (refs, t) in ways.items()
    ]
    fp = box(0, -0.5, 10, 9.5)
    (b,), res = resolve_topology([_new(fp)], env, RULES)
    assert {(a.feature.osm_id, a.include) for a in res.adjustments} == {(300, True), (301, False)}

    edits = plan_edits(res.adjustments, xml, CRS, RULES)
    assert set(edits.ways) == {300, 301} and not edits.failed
    refs = {
        wid: [int(nd.attrib["ref"]) for nd in el.findall("nd")] for wid, el in edits.ways.items()
    }
    new_ids = {r for rs in refs.values() for r in rs if r < 0}
    assert new_ids and all(r in refs[300] and r in refs[301] for r in new_ids)  # colados
    assert {1, 2, 3, 4} <= set(refs[300]) and {2, 3, 5, 6} <= set(refs[301])  # nós mantidos
    assert edits.ways[300].attrib["action"] == "modify"
    to_crs = Transformer.from_crs("EPSG:4326", CRS, always_xy=True)

    def poly(wid: int) -> Polygon:
        pts = []
        for r in refs[wid]:
            n = edits.nodes[r]
            pts.append(to_crs.transform(float(n.attrib["lon"]), float(n.attrib["lat"])))
        return Polygon(pts)

    assert fp.within(poly(300)) and fp.distance(poly(301)) > 0.05
    assert poly(300).intersection(poly(301)).area < 0.01


def test_failed_adjustment_drops_the_other_landuse_too() -> None:
    nodes = {1: (-50, 50), 2: (-50, 0), 3: (50, 0), 4: (50, 50), 5: (-50, -50), 6: (50, -50)}
    ways = {300: ([1, 2, 3, 4, 1], {"landuse": "residential"})}  # o agrícola foi apagado
    env = Surroundings()
    env.landuse.features += [
        Feature(
            Polygon([nodes[i] for i in [1, 2, 3, 4]]), "landuse=residential (way/300)", "way", 300
        ),
        Feature(
            Polygon([nodes[i] for i in [2, 5, 6, 3]]), "landuse=farmland (way/301)", "way", 301
        ),
    ]
    (_,), res = resolve_topology([_new(box(0, -0.5, 10, 9.5))], env, RULES)
    edits = plan_edits(res.adjustments, _osm_xml(nodes, ways), CRS, RULES)
    assert not edits.ways and "new/1" in edits.failed


def test_existing_building_way_loses_overlap_and_unused_node_is_deleted() -> None:
    # Edifício do OSM com um canto (nó 13) 0,4 m dentro do novo: o canto sai do anel.
    nodes = {10: (9.6, 2), 11: (20, 2), 12: (20, 12), 13: (9.6, 12)}
    xml = _osm_xml(nodes, {100: ([10, 11, 12, 13, 10], {"building": "house"})})
    osm = Polygon([nodes[i] for i in (10, 11, 12, 13)])
    (_,), res = resolve_topology([_new(box(0, 0, 10, 10))], _env(buildings=(osm,)), RULES)
    edits = plan_edits(res.adjustments, xml, CRS, RULES)
    assert set(edits.ways) == {100}
    assert edits.nodes[10].attrib.get("action") == "delete"  # dentro do novo, sem uso
    assert "13" in [nd.attrib["ref"] for nd in edits.ways[100].findall("nd")]
