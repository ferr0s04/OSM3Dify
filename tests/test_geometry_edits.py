from __future__ import annotations

import json
from pathlib import Path

import pytest
from pyproj import Transformer

from osm3d.export.osm_edits import plan_edits
from osm3d.geometry_edits import (
    ContextIndex,
    GeometryEdits,
    apply_to_context,
    edited_landuse,
    landuse_ring,
    reset_landuse,
    set_landuse,
)
from osm3d.review import ReviewStore
from tests.test_topology import CRS, RULES, _osm_xml

TO_WGS = Transformer.from_crs(CRS, "EPSG:4326", always_xy=True)
TO_CRS = Transformer.from_crs("EPSG:4326", CRS, always_xy=True)

# Residencial (y ≥ 0) e agrícola (y ≤ 0) colados em y = 0 (nós 2 e 3), e uma via que
# passa no nó 4.
NODES = {
    1: (-50, 50),
    2: (-50, 0),
    3: (50, 0),
    4: (50, 50),
    5: (-50, -50),
    6: (50, -50),
    7: (90, 50),
}
WAYS = {
    300: ([1, 2, 3, 4, 1], {"landuse": "residential"}),
    301: ([2, 5, 6, 3, 2], {"landuse": "farmland"}),
    400: ([4, 7], {"highway": "residential"}),
}


def _ll(nid: int) -> tuple[float, float]:
    lon, lat = TO_WGS.transform(*NODES[nid])
    return float(lon), float(lat)


def _context() -> dict:  # type: ignore[type-arg]
    elements = []
    for wid, (refs, tags) in WAYS.items():
        geom = [dict(zip(("lon", "lat"), _ll(r), strict=True)) for r in refs]
        elements.append({"type": "way", "id": wid, "nodes": refs, "geometry": geom, "tags": tags})
    return {"elements": elements}


def _xy(lon: float, lat: float) -> tuple[float, float]:
    x, y = TO_CRS.transform(lon, lat)
    return float(x), float(y)


def test_moving_a_shared_node_moves_it_in_every_way() -> None:
    data = _context()
    ctx = ContextIndex.of(data)
    ring = landuse_ring(ctx, GeometryEdits(), 300)
    assert [n[0] for n in ring] == [1, 2, 3, 4]
    lon, lat = TO_WGS.transform(50, -2)  # nó 3 desce 2 m
    ring[2] = (3, float(lon), float(lat))
    edits = set_landuse(ctx, GeometryEdits(), 300, ring)
    assert set(edits.nodes) == {3} and not edits.ways  # mesma sequência: só o nó movido
    assert edited_landuse(ctx, edits) == [300, 301]  # o vizinho colado também muda
    farm = {n[0]: n for n in landuse_ring(ctx, edits, 301)}
    assert _xy(farm[3][1], farm[3][2]) == pytest.approx((50, -2), abs=1e-6)
    out = apply_to_context(data, edits)
    farm_el = next(e for e in out["elements"] if e["id"] == 301)
    moved = farm_el["geometry"][farm_el["nodes"].index(3)]
    assert _xy(moved["lon"], moved["lat"]) == pytest.approx((50, -2), abs=1e-6)
    # Repor o residencial repõe o nó também no agrícola.
    assert reset_landuse(ctx, edits, 300).landuse_empty


def test_new_node_and_reset_back_to_osm() -> None:
    ctx = ContextIndex.of(_context())
    ring = landuse_ring(ctx, GeometryEdits(), 300)
    lon, lat = TO_WGS.transform(0, 55)
    ring.insert(4, (None, float(lon), float(lat)))  # entre 4 e 1, 5 m para fora
    edits = set_landuse(ctx, GeometryEdits(), 300, ring)
    assert 300 in edits.ways and edits.ways[300].before == [1, 2, 3, 4, 1]
    # Tirar o nó novo outra vez: volta a não haver edições.
    back = set_landuse(ctx, edits, 300, landuse_ring(ctx, GeometryEdits(), 300))
    assert back.landuse_empty
    with pytest.raises(ValueError):
        set_landuse(ctx, GeometryEdits(), 400, ring)  # uma via não é landuse


def test_edits_are_stored_with_the_reviews(tmp_path: Path) -> None:
    store = ReviewStore(tmp_path / "reviews.sqlite")
    ctx = ContextIndex.of(_context())
    ring = landuse_ring(ctx, GeometryEdits(), 300)
    ring.insert(4, (None, -8.0, 40.0))
    edits = set_landuse(ctx, GeometryEdits(), 300, ring)
    edits.nodes[2] = (-8.1, 40.1)
    edits.walls["wall/abc"] = [(-8.0, 40.0), (-8.001, 40.0)]
    store.save_geometry_edits(edits)
    again = store.geometry_edits()
    assert again.signature() == edits.signature()
    store.save_geometry_edits(GeometryEdits())
    assert store.geometry_edits().landuse_empty and not store.geometry_edits().walls


def test_plan_edits_applies_manual_landuse_edits() -> None:
    xml = _osm_xml(NODES, WAYS)
    # Residencial: nó 3 desce 2 m (partilhado com o agrícola) e um nó novo a norte.
    res = [(1, -50.0, 50.0), (2, -50.0, 0.0), (3, 50.0, -2.0), (4, 50.0, 50.0), (None, 0.0, 55.0)]
    farm = [(2, -50.0, 0.0), (5, -50.0, -50.0), (6, 50.0, -50.0), (3, 50.0, -2.0)]
    manual = {300: ([1, 2, 3, 4, 1], res), 301: ([2, 5, 6, 3, 2], farm)}
    edits = plan_edits([], xml, CRS, RULES, manual=manual)
    assert set(edits.ways) == {300, 301} and not edits.failed
    refs = [int(nd.attrib["ref"]) for nd in edits.ways[300].findall("nd")]
    new = [r for r in refs if r < 0]
    assert refs[:4] == [1, 2, 3, 4] and len(new) == 1 and refs[0] == refs[-1]
    node3 = edits.nodes[3]
    assert node3.attrib["action"] == "modify"
    assert _xy(float(node3.attrib["lon"]), float(node3.attrib["lat"])) == pytest.approx(
        (50, -2), abs=0.01
    )
    assert "action" not in edits.nodes[1].attrib  # não mexeu
    assert edits.way_notes[300] == ["geometria editada à mão na revisão"]


def test_plan_edits_skips_manual_edit_of_a_way_changed_in_osm() -> None:
    ways = dict(WAYS)
    ways[300] = ([1, 2, 3, 7, 4, 1], {"landuse": "residential"})  # alguém mexeu no OSM
    manual = {
        300: (
            [1, 2, 3, 4, 1],
            [(1, -50.0, 50.0), (2, -50.0, 0.0), (3, 50.0, -2.0), (4, 50.0, 50.0)],
        )
    }
    edits = plan_edits([], _osm_xml(NODES, ways), CRS, RULES, manual=manual)
    assert not edits.ways and "mudou no OSM" in edits.failed["way/300"][0]


def test_effective_context_and_api(tmp_path: Path) -> None:
    from fastapi.testclient import TestClient

    from osm3d.pipeline import context_path, effective_context_path, manual_landuse
    from tests.test_app import _app_with_candidate

    app = _app_with_candidate(tmp_path)
    svc_cfg = app.state.ctx.cfg
    client = TestClient(app)
    # O contexto da área de teste: os dois landuse e a via.
    context_path(svc_cfg).write_text(json.dumps(_context()), encoding="utf-8")
    d = client.get("/api/landuse/300").json()
    assert d["shared"] == {"2": ["landuse=farmland (way/301)"], "3": ["landuse=farmland (way/301)"],
                           "4": ["highway=residential (way/400)"]}  # fmt: skip
    nodes = d["nodes"]
    lon, lat = TO_WGS.transform(50, -2)
    nodes[2] = [3, lon, lat]
    r = client.put("/api/landuse/300/geometry", json={"nodes": nodes})
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["edited"] and out["changed"] == ["way/301"]
    assert client.get("/api/landuse/301").json()["edited"]
    eff = json.loads(effective_context_path(svc_cfg).read_text(encoding="utf-8"))
    farm = next(e for e in eff["elements"] if e["id"] == 301)
    assert _xy(**farm["geometry"][3]) == pytest.approx((50, -2), abs=1e-6)
    assert set(manual_landuse(svc_cfg)) == {300, 301}
    # Um anel que se cruza não é aceite.
    bad = [nodes[0], nodes[2], nodes[1], nodes[3]]
    assert client.put("/api/landuse/300/geometry", json={"nodes": bad}).status_code == 422
    assert not client.delete("/api/landuse/300/geometry").json()["edited"]
    assert effective_context_path(svc_cfg) == context_path(svc_cfg)
    # O mapa recebe os nós de cada way (para ligar um landuse ao nó de outra).
    feats = {f["properties"]["id"]: f for f in client.get("/api/osm-context").json()["features"]}
    assert feats["way/300"]["properties"]["nodes"] == [1, 2, 3, 4, 1]
    # O vértice do nó 4 largado no nó 7 (ponta da via): o residencial passa a usar o nó 7.
    nodes = client.get("/api/landuse/300").json()["nodes"]
    nodes[3] = [7, *_ll(7)]
    out = client.put("/api/landuse/300/geometry", json={"nodes": nodes}).json()
    assert [n[0] for n in out["nodes"]] == [1, 2, 3, 7] and "7" in out["shared"]


def test_edited_wall_links_to_landuse(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from shapely.geometry import LineString

    import osm3d.pipeline as P
    from osm3d.reconstruction.walls import WallProposal
    from tests.test_app import _app_with_candidate

    cfg = _app_with_candidate(tmp_path).state.ctx.cfg
    P.context_path(cfg).write_text(json.dumps(_context()), encoding="utf-8")
    # Muro 5 m a norte do limite norte do residencial (y = 50): sem ligação.
    wall = WallProposal("abc", {"barrier": "wall"}, LineString([(-40, 55), (40, 55)]), "x", [])
    monkeypatch.setattr(P, "wall_proposals", lambda config: [wall])
    assert P.effective_walls(cfg, GeometryEdits())[0].runs == []
    # Arrastado no mapa para 0,3 m do limite: passa a estar ligado ao landuse.
    a, b = TO_WGS.transform(-40, 50.3), TO_WGS.transform(40, 50.3)
    edits = GeometryEdits(walls={"wall/abc": [a, b]})
    (w,) = P.effective_walls(cfg, edits)
    assert w.geometry.coords[0] == pytest.approx((-40, 50.3), abs=1e-6)
    assert [r.way_id for r in w.runs] == [300]


def test_moving_a_shared_wall_vertex_saves_both_walls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fastapi.testclient import TestClient
    from shapely.geometry import LineString

    import osm3d.app.service as S
    import osm3d.pipeline as P
    from osm3d.reconstruction.walls import WallProposal
    from tests.test_app import _app_with_candidate

    app = _app_with_candidate(tmp_path)
    P.context_path(app.state.ctx.cfg).write_text(json.dumps(_context()), encoding="utf-8")
    # Dois muros ligados em (0, 70).
    a = WallProposal("aaa", {"barrier": "wall"}, LineString([(-20, 70), (0, 70)]), "x", [])
    b = WallProposal("bbb", {"barrier": "wall"}, LineString([(0, 70), (0, 90)]), "x", [])
    monkeypatch.setattr(P, "wall_proposals", lambda config: [a, b])
    monkeypatch.setattr(S, "wall_proposals", lambda config: [a, b])
    client = TestClient(app)

    def ll(*xy: tuple[float, float]) -> list[list[float]]:
        return [list(TO_WGS.transform(x, y)) for x, y in xy]

    body = {"walls": {"wall/aaa": ll((-20, 70), (0, 72)), "wall/bbb": ll((0, 72), (0, 90))}}
    res = client.put("/api/walls/geometry", json=body).json()["details"]
    assert res["wall/aaa"]["edited"] and res["wall/bbb"]["edited"]
    # Voltar ao ponto original (Ctrl+Z no mapa): deixam de estar editados.
    body = {"walls": {"wall/aaa": ll((-20, 70), (0, 70)), "wall/bbb": ll((0, 70), (0, 90))}}
    res = client.put("/api/walls/geometry", json=body).json()["details"]
    assert not res["wall/aaa"]["edited"] and not res["wall/bbb"]["edited"]
    assert (
        client.put(
            "/api/walls/geometry", json={"walls": {"wall/zzz": ll((0, 0), (1, 1))}}
        ).status_code
        == 404
    )


def test_effective_context_is_safe_with_parallel_requests(tmp_path: Path) -> None:
    import threading

    from osm3d.pipeline import context_path, effective_context_path
    from osm3d.reconstruction.walls import load_wall_context
    from tests.test_app import _app_with_candidate

    cfg = _app_with_candidate(tmp_path).state.ctx.cfg
    context_path(cfg).write_text(json.dumps(_context()), encoding="utf-8")
    errors: list[str] = []

    def request(k: int) -> None:
        try:
            edits = GeometryEdits(nodes={4: (_ll(4)[0] + (k % 2) * 1e-5, _ll(4)[1])})
            load_wall_context(effective_context_path(cfg, edits), CRS)
        except Exception as exc:  # o que no servidor era um erro 500
            errors.append(repr(exc))

    for _ in range(4):
        threads = [threading.Thread(target=request, args=(k,)) for k in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
    assert errors == []
