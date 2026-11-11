from __future__ import annotations

from pathlib import Path
from xml.etree import ElementTree as ET

import pytest
from fastapi.testclient import TestClient

from osm3d.app.server import create_app
from osm3d.config import AreaConfig, Config, PathsConfig, SourcesConfig
from osm3d.domain.models import RoofShape
from osm3d.export.geopackage import write_buildings
from osm3d.geospatial.footprints import load_footprints
from osm3d.pipeline import Stage, artefact_path
from tests.conftest import make_part
from tests.test_footprints import OSM_XML


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    cfg = Config(
        area=AreaConfig(name="teste", bbox=(-8.68, 41.27, -8.65, 41.29)),
        paths=PathsConfig(
            raw=tmp_path / "raw", processed=tmp_path / "processed", output=tmp_path / "out"
        ),
        sources=SourcesConfig(lidar_dir=tmp_path / "lidar", dtm_dir=tmp_path / "dtm"),
    )
    fetch = artefact_path(cfg, Stage.FETCH)
    fetch.parent.mkdir(parents=True)
    fetch.write_text(OSM_XML, encoding="utf-8")
    buildings = []
    for b in load_footprints(fetch, cfg.processing_crs, 20):
        part = make_part(RoofShape.GABLED).model_copy(update={"geometry": b.footprint})
        buildings.append(b.model_copy(update={"parts": [part], "ground_elevation_m": 50.0}))
    write_buildings(buildings, artefact_path(cfg, Stage.QA), cfg.processing_crs)
    return TestClient(create_app(cfg))


def test_lists_and_summary(client: TestClient) -> None:
    rows = client.get("/api/buildings").json()
    assert {r["key"] for r in rows} == {"way/10", "relation/30"}
    gj = client.get("/api/buildings.geojson").json()
    assert len(gj["features"]) == 2
    lon, lat = gj["features"][0]["geometry"]["coordinates"][0][0]
    assert -9 < lon < -8 and 41 < lat < 42  # WGS84
    s = client.get("/api/summary").json()
    assert s["n_buildings"] == 2 and s["status"] == {"pending": 2}
    assert s["evaluation"]["n_reference"] == 1  # way/10 tem roof:shape=hipped


def test_detail_mesh_points(client: TestClient) -> None:
    d = client.get("/api/buildings/way/10").json()
    assert d["tags"]["conflicts"]["roof:shape"] == {"osm": "hipped", "proposed": "gabled"}
    assert d["effective"]["roof_shape"] == "gabled"
    mesh = client.get("/api/buildings/way/10/mesh").json()
    assert mesh["roof"]["faces"] and mesh["walls"]["faces"] and mesh["footprint"]
    pts = client.get("/api/buildings/way/10/points").json()
    assert pts["count"] == 0  # sem LAZ no teste
    assert client.get("/api/buildings/way/999").status_code == 404
    assert client.get("/api/buildings/node/10").status_code == 404


def test_preview_review_export(client: TestClient) -> None:
    ov = {"roof_shape": "hipped", "height": 11.0}
    p = client.post("/api/buildings/way/10/preview", json=ov).json()
    assert p["tags"]["proposed"]["roof:shape"] == "hipped"
    assert p["tags"]["proposed"]["height"] == "11"

    r = client.put(
        "/api/buildings/way/10/review",
        json={"status": "accepted", "note": "visto", "overrides": ov},
    ).json()
    assert r["status"] == "accepted" and r["edited"] and r["note"] == "visto"
    assert r["effective"]["height"] == 11.0
    client.put("/api/buildings/relation/30/review", json={"status": "rejected"})

    out = client.post("/api/export").json()
    assert out["exported"] == 1
    root = ET.parse(out["path"]).getroot()
    way = root.find("way[@id='10']")
    assert way is not None
    tags = {t.attrib["k"]: t.attrib["v"] for t in way.findall("tag")}
    assert tags["height"] == "11" and tags["roof:shape"] == "hipped"  # sem conflito: igual
    assert root.find("relation[@id='30']") is None  # rejeitado

    d = client.delete("/api/buildings/way/10/review").json()
    assert d["status"] == "pending" and not d["edited"]


def test_invalid_review_rejected(client: TestClient) -> None:
    bad = client.put("/api/buildings/way/10/review", json={"status": "talvez"})
    assert bad.status_code == 422
    bad = client.post("/api/buildings/way/10/preview", json={"height": -3})
    assert bad.status_code == 422


def test_frontend_served(client: TestClient) -> None:
    html = client.get("/").text
    assert "osm3d" in html and "/static/app.js" in html
    assert client.get("/static/app.js").status_code == 200
