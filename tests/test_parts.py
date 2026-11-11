from __future__ import annotations

from pathlib import Path
from xml.etree import ElementTree as ET

import pytest
from shapely.geometry import Polygon, box

from osm3d.domain.models import Building, RoofShape
from osm3d.export.osm_xml import plan_export, write_josm_file
from osm3d.export.s3db_tags import TagPolicy
from osm3d.geospatial.footprints import load_footprints
from osm3d.parts import (
    layout_from_polygons,
    layout_polygons,
    merge_parts,
    split_part,
    suggest_layout,
    validate_layout,
    vertex_info,
)
from tests.conftest import make_part
from tests.test_footprints import OSM_XML

RECT = box(0, 0, 20, 10)


def _shared(layout, i: int, j: int) -> set[int]:  # type: ignore[no-untyped-def]
    return set(layout.parts[i]) & set(layout.parts[j])


def test_suggest_side_cut_shares_vertices_on_outline() -> None:
    # Volume principal: 73 % de cima; alpendre em baixo (caso way/330892659).
    layout = suggest_layout(RECT, box(-1, 2.7, 21, 11))
    assert layout is not None and len(layout.parts) == 2
    assert len(layout.vertices) == 6  # 4 cantos + 2 pontos do corte
    shared = _shared(layout, 0, 1)
    assert len(shared) == 2
    kinds = vertex_info(layout, RECT)
    assert sorted(k.kind for k in kinds) == ["edge", "edge"] + ["outline"] * 4
    assert all(kinds[k].kind == "edge" for k in shared)
    assert validate_layout(layout, RECT) == []


def test_suggest_corner_cut_l_shaped_rest() -> None:
    # Volume principal recortado num lado e numa ponta: o resto é um L.
    layout = suggest_layout(RECT, box(-1, 3, 15, 11))
    assert layout is not None and len(layout.parts) == 2
    kinds = vertex_info(layout, RECT)
    shared = _shared(layout, 0, 1)
    assert len(shared) == 3  # dois no contorno + o canto interior
    assert sorted(kinds[k].kind for k in shared) == ["edge", "edge", "free"]
    assert validate_layout(layout, RECT) == []


def test_t_junctions_are_inserted() -> None:
    # Três partes: o vértice (10, 5) toca no lado da parte de baixo → inserido nela.
    polys = [box(0, 0, 20, 5), box(0, 5, 10, 10), box(10, 5, 20, 10)]
    layout = layout_from_polygons(polys)
    bottom_ring = [layout.vertices[k] for k in layout.parts[0]]
    assert (10.0, 5.0) in bottom_ring
    assert validate_layout(layout, RECT) == []


def test_spikes_are_removed() -> None:
    from osm3d.parts import _drop_collinear_duplicates

    # Caso real way/330892659: [7, 0, 1, 0, 6, …] com uma agulha 0 → 1 → 0.
    assert _drop_collinear_duplicates([7, 0, 1, 0, 6, 8, 9]) == [7, 0, 6, 8, 9]
    assert _drop_collinear_duplicates([1, 2, 2, 3, 1]) == [1, 2, 3]
    assert _drop_collinear_duplicates([5, 1, 2, 3, 1]) == [1, 2, 3]  # agulha a cruzar o fecho


def test_near_collinear_cut_gives_valid_parts() -> None:
    # Corte quase paralelo a um lado (desvio de 4 mm): sem agulhas nem partes inválidas.
    footprint = Polygon([(0, 0), (20, 0), (20, 10), (0, 10)])
    region = Polygon([(-1, 3.0), (21, 3.004), (21, 11), (-1, 11)])
    layout = suggest_layout(footprint, region)
    assert layout is not None
    assert validate_layout(layout, footprint) == []


def test_split_and_merge_roundtrip() -> None:
    layout = layout_from_polygons([RECT])
    two = split_part(layout, 0, (8, -1), (8, 11))
    assert len(two.parts) == 2
    areas = sorted(round(p.area) for p in layout_polygons(two))
    assert areas == [80, 120]
    # Dividir a parte da direita outra vez: o novo vértice no lado comum fica partilhado.
    right = max(range(2), key=lambda i: two.polygon(i).area)
    three = split_part(two, right, (8, 5), (20, 5))
    assert len(three.parts) == 3 and validate_layout(three, RECT) == []
    merged = merge_parts(three, 0)
    assert len(merged.parts) == 2 and validate_layout(merged, RECT) == []


def test_validate_reports_problems() -> None:
    overlap = layout_from_polygons([box(0, 0, 12, 10), box(10, 0, 20, 10)])
    assert any("sobrepõem" in p for p in validate_layout(overlap, RECT))
    gap = layout_from_polygons([box(0, 0, 8, 10), box(10, 0, 20, 10)])
    assert any("sem parte" in p for p in validate_layout(gap, RECT))


def test_split_errors() -> None:
    layout = layout_from_polygons([RECT])
    with pytest.raises(ValueError):
        split_part(layout, 0, (5, 5), (5.01, 5))
    with pytest.raises(ValueError):
        split_part(layout, 0, (30, 0), (30, 10))  # não atravessa


# ----------------------------------------------------------------- exportação


@pytest.fixture
def source(tmp_path: Path) -> Path:
    p = tmp_path / "area.osm"
    p.write_text(OSM_XML, encoding="utf-8")
    return p


def test_export_parts_topology(source: Path, tmp_path: Path) -> None:
    house = next(b for b in load_footprints(source, "EPSG:3763", 20) if b.osm_id == 10)
    minx, miny, maxx, maxy = house.footprint.bounds
    cut_y = miny + 0.6 * (maxy - miny)
    layout = suggest_layout(house.footprint, box(minx - 1, cut_y, maxx + 1, maxy + 1))
    assert layout is not None
    main = make_part(RoofShape.GABLED, height=8.0).model_copy(
        update={"geometry": layout.polygon(0)}
    )
    porch = make_part(
        RoofShape.SKILLION, height=3.0, ridge_azimuth_deg=None, facing_azimuth_deg=180
    )
    porch = porch.model_copy(update={"geometry": layout.polygon(1)})
    b: Building = house.model_copy(
        update={
            "parts": [main, porch],
            "layout": layout.model_copy(update={"fitted": [main, porch]}),
            "review_status": "accepted",
        }
    )
    selected, records = plan_export([b], TagPolicy())
    assert records[0].reason == "ok (2 partes)"
    out = write_josm_file(selected, source, tmp_path / "out.osm", crs="EPSG:3763")
    root = ET.parse(out).getroot()

    outline = root.find("way[@id='10']")
    assert outline is not None and outline.attrib["action"] == "modify"
    refs = [int(nd.attrib["ref"]) for nd in outline.findall("nd")]
    new_on_outline = [r for r in refs if r < 0]
    assert len(refs) == 7 and refs[0] == refs[-1]  # 4 nós + 2 inseridos + fecho
    assert len(new_on_outline) == 2
    outline_tags = {t.attrib["k"]: t.attrib["v"] for t in outline.findall("tag")}
    assert outline_tags["height"] == "8"  # altura total = parte mais alta

    parts = [w for w in root.findall("way") if int(w.attrib["id"]) < 0]
    assert len(parts) == 2
    rings = [[int(nd.attrib["ref"]) for nd in w.findall("nd")] for w in parts]
    # As duas partes partilham exatamente os dois nós novos do corte, que estão no contorno.
    shared = set(rings[0]) & set(rings[1])
    assert shared == set(new_on_outline)
    # Os cantos das partes são os nós originais do contorno (1–4).
    assert {r for ring in rings for r in ring if r > 0} == {1, 2, 3, 4}
    tags = [{t.attrib["k"]: t.attrib["v"] for t in w.findall("tag")} for w in parts]
    assert all(t["building:part"] == "yes" for t in tags)
    assert {t["roof:shape"] for t in tags} == {"gabled", "skillion"}
    # Os nós novos existem no ficheiro, com coordenadas.
    node_ids = {int(n.attrib["id"]) for n in root.findall("node")}
    assert set(new_on_outline) <= node_ids


def test_relation_with_parts_is_not_exported(source: Path) -> None:
    rel = next(b for b in load_footprints(source, "EPSG:3763", 20) if b.osm_type == "relation")
    layout = layout_from_polygons([Polygon(rel.footprint.exterior.coords)], source="manual")
    two = split_part(
        layout,
        0,
        rel.footprint.centroid.coords[0],
        (rel.footprint.centroid.x, rel.footprint.centroid.y + 1),
    )
    parts = [
        make_part().model_copy(update={"geometry": two.polygon(i)}) for i in range(len(two.parts))
    ]
    b = rel.model_copy(
        update={
            "parts": parts,
            "layout": two.model_copy(update={"fitted": parts}),
            "review_status": "accepted",
        }
    )
    selected, records = plan_export([b], TagPolicy())
    assert selected == [] and "multipolígonos" in records[0].reason
