from __future__ import annotations

from pathlib import Path
from xml.etree import ElementTree as ET

import pytest

from osm3d.domain.models import Building, QAFlag, RoofShape
from osm3d.export.osm_xml import plan_export, write_josm_file, write_report
from osm3d.export.s3db_tags import TagPolicy
from osm3d.geospatial.footprints import load_footprints
from tests.conftest import make_part
from tests.test_footprints import OSM_XML


@pytest.fixture
def source(tmp_path: Path) -> Path:
    p = tmp_path / "area.osm"
    p.write_text(OSM_XML, encoding="utf-8")
    return p


def _with_part(b: Building, **update: object) -> Building:
    return b.model_copy(update={"parts": [make_part(RoofShape.GABLED)], **update})


def test_plan_and_write(source: Path, tmp_path: Path) -> None:
    by_id = {b.osm_id: b for b in load_footprints(source, "EPSG:3763", 20)}
    house = _with_part(by_id[10])  # way, já tem roof:shape=hipped → conflito
    courtyard = _with_part(by_id[30])  # relação multipolígono
    flagged = _with_part(
        by_id[30].model_copy(update={"osm_id": 31}),
        qa_flags=[QAFlag(code="roof_shape_unknown", message="x")],
    )
    selected, records = plan_export([house, courtyard, flagged], TagPolicy(), include_pending=True)

    assert [b.key for b, _ in selected] == ["way/10", "relation/30"]
    rec = {r.key: r for r in records}
    assert rec["way/10"].conflicts == {"roof:shape": ("hipped", "gabled")}
    assert rec["way/10"].added["height"] == "8.5"
    assert not rec["relation/31"].exported and "roof_shape_unknown" in rec["relation/31"].reason

    out = write_josm_file(selected, source, tmp_path / "out.osm")
    root = ET.parse(out).getroot()
    assert "upload" not in root.attrib  # upload permitido (omissão do JOSM)
    blocked = write_josm_file(selected, source, tmp_path / "blocked.osm", upload="never")
    assert ET.parse(blocked).getroot().attrib["upload"] == "never"
    way = root.find("way[@id='10']")
    assert way is not None and way.attrib["action"] == "modify" and way.attrib["version"] == "3"
    tags = {t.attrib["k"]: t.attrib["v"] for t in way.findall("tag")}
    assert tags["roof:shape"] == "hipped"  # valor humano mantido
    assert tags["height"] == "8.5"
    rel = root.find("relation[@id='30']")
    assert rel is not None and rel.attrib["action"] == "modify"
    # Ways-membro e nós presentes, sem action (não modificados).
    for wid in ("21", "22", "23"):
        member = root.find(f"way[@id='{wid}']")
        assert member is not None and "action" not in member.attrib
    node_ids = {n.attrib["id"] for n in root.findall("node")}
    for w in root.findall("way"):
        assert {nd.attrib["ref"] for nd in w.findall("nd")} <= node_ids

    report = write_report(records, tmp_path / "r.csv").read_text(encoding="utf-8")
    assert "OSM hipped / proposto gabled" in report


def test_rejected_and_accepted(source: Path) -> None:
    b = _with_part(load_footprints(source, "EPSG:3763", 20)[0])
    rejected = b.model_copy(update={"review_status": "rejected"})
    accepted_flagged = b.model_copy(
        update={"review_status": "accepted", "qa_flags": [QAFlag(code="x", message="y")]}
    )
    sel, _ = plan_export([rejected], TagPolicy(), include_pending=True)
    assert sel == []
    sel, _ = plan_export([b], TagPolicy())  # por omissão: só aceites
    assert sel == []
    sel, _ = plan_export([b.model_copy(update={"review_status": "accepted"})], TagPolicy())
    assert len(sel) == 1
    sel, _ = plan_export([accepted_flagged], TagPolicy(), include_pending=False)
    assert len(sel) == 1  # aceite por um humano: exporta mesmo com sinalizações
    sel, _ = plan_export([b], TagPolicy(), include_pending=False)
    assert sel == []


def test_contiguous_new_buildings_share_nodes(source: Path, tmp_path: Path) -> None:
    import numpy as np
    import shapely
    from shapely.geometry import box

    origin = np.array([-45000.0, 182000.0])
    a = shapely.affinity.translate(box(0, 0, 10, 8), *origin)
    b = shapely.affinity.translate(box(10, 0, 16, 5), *origin)  # T-junção em (10, 5)
    sel = [
        (Building(osm_type="new", osm_id=i, osm_version=0, footprint=g), {"building": "yes"})
        for i, g in enumerate((a, b), start=1)
    ]
    root = ET.parse(write_josm_file(sel, source, tmp_path / "out.osm")).getroot()
    ways = [[nd.attrib["ref"] for nd in w.findall("nd")] for w in root.findall("way")]
    assert len(ways) == 2
    shared = set(ways[0]) & set(ways[1])
    assert len(shared) == 2  # (10, 0) e a junção em T (10, 5), inserida no lado de a


def test_new_building_with_parts_shares_nodes(source: Path, tmp_path: Path) -> None:
    import numpy as np
    import shapely
    from shapely.geometry import box

    from osm3d.parts import layout_from_polygons

    o = np.array([-45000.0, 182000.0])
    main = shapely.affinity.translate(box(4, 0, 14, 20), *o)
    lean = shapely.affinity.translate(box(0, 4, 4, 14), *o)  # alpendre a oeste
    outline = shapely.union_all([main, lean])
    layout = layout_from_polygons([main, lean], source="suggested")
    parts = [make_part(RoofShape.GABLED), make_part(RoofShape.SKILLION)]
    b = Building(
        osm_type="new",
        osm_id=1,
        osm_version=0,
        footprint=outline,
        existing_tags={"building": "yes"},
        layout=layout,
        parts=parts,
        review_status="accepted",
    )
    selected, records = plan_export([b], TagPolicy())
    assert records[0].exported and "2 partes" in records[0].reason
    root = ET.parse(write_josm_file(selected, source, tmp_path / "out.osm")).getroot()
    ways = root.findall("way")
    assert len(ways) == 3
    kinds = [{t.attrib["k"] for t in w.findall("tag")} for w in ways]
    assert sum("building:part" in k for k in kinds) == 2
    refs = [{nd.attrib["ref"] for nd in w.findall("nd")} for w in ways]
    outline_refs = next(r for r, k in zip(refs, kinds, strict=True) if "building" in k)
    for r, k in zip(refs, kinds, strict=True):
        if "building:part" in k:
            assert len(r & outline_refs) >= 3  # parte presa ao contorno


def _halves(fp):  # type: ignore[no-untyped-def]
    from shapely.geometry import box

    from osm3d.parts import layout_from_polygons

    x0, y0, x1, y1 = fp.bounds
    mid = (x0 + x1) / 2
    left = fp.intersection(box(x0 - 1, y0 - 1, mid, y1 + 1))
    right = fp.intersection(box(mid, y0 - 1, x1 + 1, y1 + 1))
    return layout_from_polygons([left, right], source="manual").model_copy(
        update={"split": "buildings"}
    )


def test_new_building_split_into_buildings(source: Path, tmp_path: Path) -> None:
    import shapely
    from shapely.geometry import box

    from osm3d.export.osm_xml import has_parts, is_split

    fp = shapely.affinity.translate(box(0, 0, 20, 10), -45000.0, 182000.0)
    parts = [make_part(RoofShape.GABLED), make_part(RoofShape.FLAT)]
    b = Building(
        osm_type="new", osm_id=1, osm_version=0, footprint=fp,
        existing_tags={"building": "house"}, layout=_halves(fp), parts=parts,
        review_status="accepted",
    )  # fmt: skip
    assert is_split(b) and not has_parts(b)
    selected, records = plan_export([b], TagPolicy())
    assert "dividido em 2 edifícios" in records[0].reason
    root = ET.parse(write_josm_file(selected, source, tmp_path / "out.osm")).getroot()
    ways = root.findall("way")
    tags = [{t.attrib["k"]: t.attrib["v"] for t in w.findall("tag")} for w in ways]
    assert len(ways) == 2 and all(t["building"] == "house" for t in tags)
    assert not any("building:part" in t for t in tags)
    assert sorted(t["roof:shape"] for t in tags) == ["flat", "gabled"]
    refs = [{nd.attrib["ref"] for nd in w.findall("nd")} for w in ways]
    assert len(refs[0] & refs[1]) == 2  # a parede comum


def test_osm_way_split_into_buildings(source: Path, tmp_path: Path) -> None:
    house = {b.osm_id: b for b in load_footprints(source, "EPSG:3763", 20)}[10]
    b = house.model_copy(
        update={
            "layout": _halves(house.footprint),
            "parts": [make_part(RoofShape.GABLED), make_part(RoofShape.FLAT)],
            "review_status": "accepted",
        }
    )
    selected, _ = plan_export([b], TagPolicy())
    root = ET.parse(write_josm_file(selected, source, tmp_path / "out.osm")).getroot()
    way = root.find("way[@id='10']")
    assert way is not None and way.attrib["action"] == "modify"
    new = [w for w in root.findall("way") if int(w.attrib["id"]) < 0]
    assert len(new) == 1
    old_tags = {t.attrib["k"]: t.attrib["v"] for t in way.findall("tag")}
    new_tags = {t.attrib["k"]: t.attrib["v"] for t in new[0].findall("tag")}
    assert old_tags["roof:shape"] == "hipped"  # o valor humano fica na way original
    assert new_tags["building"] == "house" and new_tags["roof:shape"] == "flat"
    a = [nd.attrib["ref"] for nd in way.findall("nd")]
    c = [nd.attrib["ref"] for nd in new[0].findall("nd")]
    assert a[0] == a[-1] and c[0] == c[-1]
    assert len(set(a) & set(c)) == 2  # a parede comum: nós novos partilhados
    assert {"1", "2", "3", "4"} <= set(a) | set(c)  # os nós do contorno mantêm-se
