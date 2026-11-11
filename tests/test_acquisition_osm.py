from __future__ import annotations

from osm3d.acquisition.osm import build_query

BBOX = (-8.69911, 41.27348, -8.63541, 41.29553)


def test_bbox_query_uses_south_west_north_east_order() -> None:
    query = build_query(BBOX)
    assert 'way["building"](41.27348,-8.69911,41.29553,-8.63541);' in query
    assert "area(" not in query
    assert "out meta;" in query


def test_relation_query_filters_by_area() -> None:
    query = build_query(BBOX, osm_relation_id=5989576)
    assert "area(3605989576)->.a;" in query
    assert 'way["building"](area.a)(41.27348,-8.69911,41.29553,-8.63541);' in query
