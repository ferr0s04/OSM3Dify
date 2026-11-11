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
        # A way 40 do ficheiro de teste é uma building:part dentro de way/10; aqui
        # ignora-se para testar a divisão (o bloqueio tem teste próprio).
        update = {"parts": [part], "ground_elevation_m": 50.0, "osm_parts": []}
        buildings.append(b.model_copy(update=update))
    write_buildings(buildings, artefact_path(cfg, Stage.QA), cfg.processing_crs)
    return TestClient(create_app(cfg))


def test_lists_and_summary(client: TestClient) -> None:
    rows = client.get("/api/buildings").json()
    assert {r["key"] for r in rows} == {"way/10", "relation/30"}
    # Alturas mostradas arredondadas a 0,5 m, como na exportação (8,3 → 8,5; 2,2 → 2).
    assert {r["height"] for r in rows} == {8.5} and {r["roof_height"] for r in rows} == {2.0}
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
    # Topo da malha = altura arredondada (relativa ao chão).
    assert max(mesh["roof"]["vertices"][2::3]) <= 8.5 + 1e-6
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


def test_archive_accepted_and_rejected(client: TestClient) -> None:
    client.put("/api/buildings/way/10/review", json={"status": "accepted"})
    client.put("/api/buildings/relation/30/review", json={"status": "rejected"})

    r = client.post("/api/reviews/archive", json={"status": "accepted"}).json()
    assert r["changed"] == 1
    rows = {x["key"]: x for x in client.get("/api/buildings").json()}
    assert rows["way/10"]["archived"] and rows["way/10"]["status"] == "accepted"
    assert not rows["relation/30"]["archived"]
    s = client.get("/api/summary").json()
    assert s["archived"] == {"accepted": 1} and s["status"] == {"rejected": 1}
    # Arquivado = já submetido: não volta a ser exportado.
    assert client.post("/api/export").json()["exported"] == 0

    client.post("/api/reviews/archive", json={"status": "rejected"})
    assert all(x["archived"] for x in client.get("/api/buildings").json())
    # Desarquivar aceites devolve-os à exportação.
    client.post("/api/reviews/archive", json={"status": "accepted", "archived": False})
    assert client.post("/api/export").json()["exported"] == 1
    assert client.post("/api/reviews/archive", json={"status": "pending"}).status_code == 422


def test_archived_review_survives_new_osm_version(tmp_path: Path) -> None:
    from shapely.geometry import box

    from osm3d.domain.models import Building
    from osm3d.review import Review, apply_review

    b = Building(osm_type="way", osm_id=1, osm_version=5, footprint=box(0, 0, 10, 10))
    old = Review(osm_key="way/1", osm_version=4, status="accepted")
    assert apply_review(b, old).review_status == "pending"  # desatualizado
    archived = old.model_copy(update={"archived_at": old.reviewed_at})
    got = apply_review(b, archived)
    assert got.review_status == "accepted" and got.review_archived


def test_store_migrates_old_schema(tmp_path: Path) -> None:
    import sqlite3

    from osm3d.review import ReviewStore

    path = tmp_path / "old.sqlite"
    with sqlite3.connect(path) as c:
        c.execute(
            "CREATE TABLE reviews (osm_key TEXT PRIMARY KEY, osm_version INTEGER NOT NULL,"
            " status TEXT NOT NULL, note TEXT, overrides TEXT NOT NULL, reviewed_at TEXT NOT NULL)"
        )
        c.execute(
            "INSERT INTO reviews VALUES ('way/1', 1, 'accepted', NULL, '{}', '2026-11-11T10:00:00')"
        )
    store = ReviewStore(path)
    assert not store.all()["way/1"].archived
    assert store.set_archived("accepted", True) == 1
    assert store.all()["way/1"].archived


def test_parts_workflow(client: TestClient) -> None:
    base = "/api/buildings/way/10"
    view = client.get(f"{base}/parts").json()
    assert view["editable"] and len(view["outline"]) == 4 and view["layout"] is None
    # Começar manualmente (contorno = 1 parte) e dividir ao meio, de lado a lado.
    layout = client.post(f"{base}/parts/start").json()["layout"]
    assert len(layout["parts"]) == 1
    xs = [p[0] for p in view["outline"]]
    ys = [p[1] for p in view["outline"]]
    mid = (min(xs) + max(xs)) / 2
    split = client.post(
        f"{base}/parts/split",
        json={"layout": layout, "part": 0, "a": [mid, min(ys)], "b": [mid, max(ys)]},
    ).json()["layout"]
    assert len(split["parts"]) == 2 and len(split["vertices"]) == 6

    prev = client.post(
        f"{base}/parts/preview",
        json={"layout": split, "overrides": [{"roof_shape": "flat", "height": 3.0}, {}]},
    ).json()
    assert prev["problems"] == []
    assert len(prev["parts"]) == 2 and prev["parts"][0]["tags"]["building:part"] == "yes"
    assert prev["parts"][0]["tags"]["roof:shape"] == "flat"
    assert len(prev["mesh"]["parts"]) == 2

    saved = client.put(
        f"{base}/review",
        json={
            "status": "accepted",
            "parts": {"layout": split, "overrides": [{"roof_shape": "flat", "height": 3.0}, {}]},
        },
    ).json()
    assert saved["has_layout"] and saved["n_parts"] == 2
    assert client.get(f"{base}/parts").json()["layout"] is not None

    out = client.post("/api/export").json()
    assert out["exported"] == 1
    root = ET.parse(out["path"]).getroot()
    new_ways = [w for w in root.findall("way") if int(w.attrib["id"]) < 0]
    assert len(new_ways) == 2
    outline = root.find("way[@id='10']")
    assert outline is not None and len(outline.findall("nd")) == 7  # 2 nós do corte inseridos

    merged = client.post(f"{base}/parts/merge", json={"layout": split, "part": 0}).json()["layout"]
    assert len(merged["parts"]) == 1


def test_scene_for_map(client: TestClient) -> None:
    scene = client.get("/api/scene").json()
    lon, lat = scene["origin"]
    assert -9 < lon < -8 and 41 < lat < 42
    keys = {b["key"] for b in scene["buildings"]}
    assert keys == {"way/10", "relation/30"}
    part = scene["buildings"][0]["parts"][0]
    assert part["roof"]["f"] and part["walls"]["f"] and part["roof_colour"].startswith("#")
    zs = part["roof"]["v"][2::3]
    assert 0 < max(zs) <= 8.5 + 1e-6  # alturas acima do terreno, arredondadas
    one = client.get("/api/buildings/way/10/scene").json()
    assert one["key"] == "way/10"


def test_parts_validation_errors(client: TestClient) -> None:
    base = "/api/buildings/way/10"
    bad = {"vertices": [[0, 0], [1, 0]], "parts": [[0, 1, 5]]}
    assert client.post(f"{base}/parts/preview", json={"layout": bad}).status_code == 422
    # Uma só parte não é uma divisão: guardar falha com explicação.
    one = client.post(f"{base}/parts/start").json()["layout"]
    r = client.put(f"{base}/review", json={"status": "accepted", "parts": {"layout": one}})
    assert r.status_code == 422 and "pelo menos 2 partes" in r.json()["detail"]
    # Multipolígonos não são divisíveis.
    assert client.post("/api/buildings/relation/30/parts/start").status_code == 422


def test_existing_osm_parts_block_editing() -> None:
    from shapely.geometry import box

    from osm3d.app.service import ReviewService
    from osm3d.domain.models import Building

    b = Building(
        osm_type="way",
        osm_id=1,
        osm_version=1,
        footprint=box(0, 0, 10, 10),
        ground_elevation_m=0.0,
        osm_parts=[40],
    )
    why = ReviewService._editable(None, b)  # type: ignore[arg-type]
    assert why is not None and "building:part" in why


def test_invalid_review_rejected(client: TestClient) -> None:
    bad = client.put("/api/buildings/way/10/review", json={"status": "talvez"})
    assert bad.status_code == 422
    bad = client.post("/api/buildings/way/10/preview", json={"height": -3})
    assert bad.status_code == 422


def test_osm_change_and_colours(client: TestClient) -> None:
    from osm3d.app.service import display_colour

    rows = {r["key"]: r for r in client.get("/api/buildings").json()}
    # way/10 tem roof:shape=hipped no OSM e a proposta é gabled → diverge.
    assert rows["way/10"]["osm3d"] and rows["way/10"]["change"] == "diverge"
    assert not rows["relation/30"]["osm3d"] and rows["relation/30"]["change"] == "novo"
    d = client.get("/api/buildings/way/10").json()
    assert d["osm_change"]["existing_3d"] == {"roof:shape": "hipped"}
    assert d["colours"]["walls_source"] == "omissão"
    assert display_colour("red", "#000000") == "#ff0000"
    assert display_colour("light_gray", "#000000") == "#d3d3d3"
    assert display_colour("cor-inventada", "#123456") == "#123456"


def test_frontend_served(client: TestClient) -> None:
    html = client.get("/").text
    assert "osm3d" in html and "/static/app.js" in html
    js = client.get("/static/app.js")
    assert js.status_code == 200
    # Frontend sempre revalidado: evita misturar módulos antigos e novos após atualizações.
    assert js.headers["cache-control"] == "no-cache"
    assert client.get("/").headers["cache-control"] == "no-cache"
    assert "cache-control" not in client.get("/api/summary").headers
