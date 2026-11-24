from __future__ import annotations

from xml.etree import ElementTree as ET

from pyproj import Transformer
from shapely.geometry import LineString, Polygon

from osm3d.acquisition.municipal import MunicipalBarrier, parse_barriers
from osm3d.config import BarrierLayer, BarrierRule
from osm3d.export.osm_edits import plan_edits
from osm3d.reconstruction.walls import (
    OsmLanduseRing,
    WallContext,
    WallProposal,
    propose_walls,
    run_faces,
    with_run_vertices,
)
from osm3d.validation.topology import Adjustment, Feature, TopologyRules

CRS = "EPSG:3763"
TO_WGS = Transformer.from_crs(CRS, "EPSG:4326", always_xy=True)
TO_CRS = Transformer.from_crs("EPSG:4326", CRS, always_xy=True)

RES = Polygon([(-50, 0), (50, 0), (50, 50), (-50, 50)])
FARM = Polygon([(-50, -50), (50, -50), (50, 0), (-50, 0)])


def _barrier(key: str, coords, prio: int = 0, kind: str = "wall") -> MunicipalBarrier:  # type: ignore[no-untyped-def]
    return MunicipalBarrier(key, f"camada {prio}", LineString(coords), {"barrier": kind}, prio)


def _ctx(**kw) -> WallContext:  # type: ignore[no-untyped-def]
    ctx = WallContext(has_barriers=True, **kw)
    return ctx


def test_layer_rules_map_attributes_to_tags() -> None:
    layer = BarrierLayer(
        label="2024",
        feature_class="x:y",
        rules=[
            BarrierRule(
                match={"valor_cons": "12", "suporte": "T"}, tags={"barrier": "wall", "x": "1"}
            ),
            BarrierRule(match={"valor_cons": "12"}, tags={"barrier": "wall"}),
        ],
    )
    xml = b"""<FeatureSet>
      <Feature><Property><Name>gid</Name><Value>7</Value></Property>
        <Property><Name>valor_cons</Name><Value>12</Value></Property>
        <Property><Name>suporte</Name><Value>T</Value></Property>
        <Property><Name>geom</Name>
          <Value>MULTILINESTRING ((0 0, 10 0), (20 0, 30 0))</Value></Property></Feature>
      <Feature><Property><Name>gid</Name><Value>8</Value></Property>
        <Property><Name>valor_cons</Name><Value>99</Value></Property>
        <Property><Name>geom</Name><Value>LINESTRING (0 5, 10 5)</Value></Property></Feature>
    </FeatureSet>"""
    out = parse_barriers(xml, layer, 0)
    # A primeira regra que bate manda.
    assert [(b.key, b.tags) for b in out] == [
        ("0:7:0", {"barrier": "wall", "x": "1"}),
        ("0:7:1", {"barrier": "wall", "x": "1"}),
    ]


def test_same_wall_in_two_layers_and_already_in_osm() -> None:
    new = _barrier("0:1:0", [(0, 20), (30, 20)])
    old_same = _barrier("1:5:0", [(0, 20.8), (30, 20.8)], prio=1)  # mesmo muro, 0,8 m ao lado
    old_extra = _barrier("1:6:0", [(0, 30), (30, 30)], prio=1)  # só na camada antiga
    mapped = _barrier("0:2:0", [(0, 40), (30, 40)])
    ctx = _ctx(barriers=[LineString([(0, 40.5), (30, 40.5)])])
    walls = propose_walls([new, old_same, old_extra, mapped], ctx)
    ys = sorted(round(w.geometry.coords[0][1]) for w in walls)
    assert ys == [20, 30]  # o da camada antiga sai onde a nova o tem; o já no OSM sai


def test_wall_along_landuse_edge_has_runs_on_both_glued_landuses() -> None:
    ctx = _ctx(
        landuse=[
            OsmLanduseRing(300, "landuse=residential (way/300)", RES),
            OsmLanduseRing(301, "landuse=farmland (way/301)", FARM),
        ]
    )
    (w,) = propose_walls([_barrier("0:1:0", [(-20, 0.6), (20, 0.6)])], ctx)
    assert {r.way_id for r in w.runs} == {300, 301}
    assert all(
        abs(r.end_m - r.start_m - 40) < 1.01 and abs(r.max_offset_m - 0.6) < 1e-6 for r in w.runs
    )
    # Um muro perpendicular ao limite não lhe fica ligado.
    (cross,) = propose_walls([_barrier("0:2:0", [(0, -10), (0, 10)])], ctx)
    assert not cross.runs


def _xml(nodes: dict[int, tuple[float, float]], ways: dict[int, tuple[list[int], dict]]) -> bytes:
    root = ET.Element("osm", version="0.6")
    for nid, (x, y) in nodes.items():
        lon, lat = TO_WGS.transform(x, y)
        ET.SubElement(root, "node", id=str(nid), version="1", lat=f"{lat:.9f}", lon=f"{lon:.9f}")
    for wid, (refs, tags) in ways.items():
        w = ET.SubElement(root, "way", id=str(wid), version="2")
        for r in refs:
            ET.SubElement(w, "nd", ref=str(r))
        for k, v in tags.items():
            ET.SubElement(w, "tag", k=k, v=v)
    return ET.tostring(root)


def test_export_moves_glued_landuse_edge_onto_the_wall_with_shared_nodes() -> None:
    nodes = {1: (-50, 50), 2: (-50, 0), 3: (50, 0), 4: (50, 50), 5: (-50, -50), 6: (50, -50)}
    ways = {
        300: ([1, 2, 3, 4, 1], {"landuse": "residential"}),
        301: ([2, 5, 6, 3, 2], {"landuse": "farmland"}),
    }
    rings = {
        300: OsmLanduseRing(300, "landuse=residential (way/300)", RES),
        301: OsmLanduseRing(301, "landuse=farmland (way/301)", FARM),
    }
    (w,) = propose_walls(
        [_barrier("0:1:0", [(-20, 0.6), (20, 0.6)])], _ctx(landuse=list(rings.values()))
    )
    adjustments = [
        Adjustment(
            w.key,
            Feature(rings[r.way_id].polygon, rings[r.way_id].label, "way", r.way_id),
            face,
            inc,
            "x",
            2.5,
        )
        for r in w.runs
        for face, inc in run_faces(w, rings[r.way_id].polygon, r)
    ]
    assert {(a.feature.osm_id, a.include) for a in adjustments} == {(300, False), (301, True)}
    edits = plan_edits(
        adjustments,
        _xml(nodes, ways),
        CRS,
        TopologyRules(),
        lines=[(w.key, with_run_vertices(w), w.tags)],
    )
    assert set(edits.ways) == {300, 301} and not edits.failed and len(edits.new_ways) == 1
    wall_refs = [int(nd.attrib["ref"]) for nd in edits.new_ways[0].findall("nd")]
    refs = {
        wid: {int(nd.attrib["ref"]) for nd in el.findall("nd")} for wid, el in edits.ways.items()
    }
    assert set(wall_refs) <= refs[300] and set(wall_refs) <= refs[301]  # o muro é o limite
    assert {2, 3} <= refs[300] and {2, 3} <= refs[301]  # os cantos do limite continuam

    def poly(wid: int) -> Polygon:
        el = edits.ways[wid]
        pts = []
        for nd in el.findall("nd"):
            n = edits.nodes[int(nd.attrib["ref"])]
            pts.append(TO_CRS.transform(float(n.attrib["lon"]), float(n.attrib["lat"])))
        return Polygon(pts)

    assert poly(300).intersection(poly(301)).area < 0.01  # sem sobreposição
    assert abs(poly(300).union(poly(301)).area - 10000) < 0.5  # sem buraco
    tags = {t.attrib["k"]: t.attrib["v"] for t in edits.new_ways[0].findall("tag")}
    assert tags == {"barrier": "wall"}


def test_wall_without_landuse_is_a_standalone_way() -> None:
    w = WallProposal("abc", {"barrier": "fence"}, LineString([(0, 0), (10, 0), (10, 5)]), "x", [])
    edits = plan_edits(
        [], b"<osm version='0.6'/>", CRS, TopologyRules(), lines=[(w.key, w.geometry, w.tags)]
    )
    assert len(edits.new_ways) == 1 and len(edits.nodes) == 3 and not edits.ways


def test_retaining_walls_are_proposed_as_walls() -> None:
    retaining = _barrier("0:1:0", [(0, 0), (20, 0)], kind="retaining_wall")
    plain = _barrier("0:2:0", [(20, 0), (40, 0)])  # continua o de suporte
    (w,) = propose_walls([retaining, plain], _ctx())
    assert w.tags == {"barrier": "wall"} and w.geometry.length == 40


def test_fences_are_not_proposed() -> None:
    fence = _barrier("0:1:0", [(0, 0), (20, 0)], kind="fence")
    wall = _barrier("0:2:0", [(0, 10), (20, 10)])
    assert [w.tags for w in propose_walls([fence, wall], _ctx())] == [{"barrier": "wall"}]
