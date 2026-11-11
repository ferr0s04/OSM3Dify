from __future__ import annotations

from pathlib import Path

import pytest

from osm3d.geospatial.footprints import load_footprints
from osm3d.geospatial.tiling import summarize_tiles, tile_of

# Pequena extração sintética perto de Vila do Conde:
# - way 10: edifício simples (~20 x 11 m) com roof:shape
# - relação 30: multipolígono com pátio, exterior dividido em duas ways (21, 22)
# - way 40: building:part (não é edifício)
# - way 50: edifício com nós em falta (ignorado)
OSM_XML = """<?xml version="1.0" encoding="UTF-8"?>
<osm version="0.6">
  <node id="1" lat="41.2800" lon="-8.6700" version="1"/>
  <node id="2" lat="41.2800" lon="-8.66976" version="1"/>
  <node id="3" lat="41.2801" lon="-8.66976" version="1"/>
  <node id="4" lat="41.2801" lon="-8.6700" version="1"/>
  <node id="11" lat="41.2850" lon="-8.6600" version="1"/>
  <node id="12" lat="41.2850" lon="-8.6594" version="1"/>
  <node id="13" lat="41.2855" lon="-8.6594" version="1"/>
  <node id="14" lat="41.2855" lon="-8.6600" version="1"/>
  <node id="15" lat="41.2852" lon="-8.6598" version="1"/>
  <node id="16" lat="41.2852" lon="-8.6596" version="1"/>
  <node id="17" lat="41.2853" lon="-8.6596" version="1"/>
  <node id="18" lat="41.2853" lon="-8.6598" version="1"/>
  <way id="10" version="3">
    <nd ref="1"/><nd ref="2"/><nd ref="3"/><nd ref="4"/><nd ref="1"/>
    <tag k="building" v="house"/><tag k="roof:shape" v="hipped"/>
  </way>
  <way id="21" version="1"><nd ref="11"/><nd ref="12"/><nd ref="13"/></way>
  <way id="22" version="1"><nd ref="13"/><nd ref="14"/><nd ref="11"/></way>
  <way id="23" version="1">
    <nd ref="15"/><nd ref="16"/><nd ref="17"/><nd ref="18"/><nd ref="15"/>
  </way>
  <relation id="30" version="2">
    <member type="way" ref="21" role="outer"/>
    <member type="way" ref="22" role="outer"/>
    <member type="way" ref="23" role="inner"/>
    <tag k="type" v="multipolygon"/><tag k="building" v="yes"/>
  </relation>
  <way id="40" version="1">
    <nd ref="1"/><nd ref="2"/><nd ref="3"/><nd ref="1"/>
    <tag k="building:part" v="yes"/>
  </way>
  <way id="50" version="1">
    <nd ref="1"/><nd ref="999"/><nd ref="3"/><nd ref="1"/>
    <tag k="building" v="yes"/>
  </way>
</osm>
"""


@pytest.fixture
def osm_file(tmp_path: Path) -> Path:
    p = tmp_path / "area.osm"
    p.write_text(OSM_XML, encoding="utf-8")
    return p


def test_load_footprints(osm_file: Path) -> None:
    buildings = {b.osm_id: b for b in load_footprints(osm_file, "EPSG:3763", min_area_m2=20)}
    assert set(buildings) == {10, 30}

    house = buildings[10]
    assert house.osm_type == "way"
    assert house.osm_version == 3
    assert house.existing_tags["roof:shape"] == "hipped"
    assert 150 < house.footprint.area < 300  # ~20 m x ~11 m

    courtyard = buildings[30]
    assert courtyard.osm_type == "relation"
    assert len(courtyard.footprint.interiors) == 1
    # Coordenadas em metros PT-TM06 (Vila do Conde: x ≈ -45 km, y ≈ 179 km).
    c = courtyard.footprint.centroid
    assert -50_000 < c.x < -40_000 and 175_000 < c.y < 185_000


def test_min_area_filter(osm_file: Path) -> None:
    ids = {b.osm_id for b in load_footprints(osm_file, "EPSG:3763", min_area_m2=1000)}
    assert ids == {30}


def test_tiles(osm_file: Path) -> None:
    buildings = load_footprints(osm_file, "EPSG:3763", min_area_m2=20)
    summaries = summarize_tiles(buildings)
    assert sum(s.n_buildings for s in summaries) == 2
    assert sum(s.roof_shapes["hipped"] for s in summaries) == 1
    key = tile_of(-45_500.0, 179_250.0)
    assert key.bounds == (-46_000.0, 179_000.0, -45_000.0, 180_000.0)
    assert key.dgt_name == "154480"
    assert str(key) == "-46000_179000"


def test_dgt_tile_names() -> None:
    # Valores da grelha oficial LiDAR2024_2025_Seccionamento.gpkg.
    assert tile_of(-44_500, 179_500).dgt_name == "155480"  # (-45000, 179000)
    assert tile_of(-43_500, 179_500).dgt_name == "156480"
    assert tile_of(-42_500, 178_500).dgt_name == "157479"
    assert tile_of(-47_500, 180_500).dgt_name == "152481"
    key = tile_of(-44_500, 179_500)
    assert type(key).from_dgt_name("155480") == key
