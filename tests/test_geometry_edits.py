from __future__ import annotations

import json
from pathlib import Path
from xml.etree import ElementTree as ET

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


def test_a_new_vertex_shared_by_two_ways_is_one_node() -> None:
    """Um vértice ligado no mapa a dois elementos (largado na aresta do vizinho) é o mesmo
    nó: no contexto editado e no .osm da exportação."""
    x, y = 0.0, 0.0  # a meio do limite comum (y = 0)
    lon, lat = TO_WGS.transform(x, y)
    ctx = ContextIndex.of(_context())
    res = landuse_ring(ctx, GeometryEdits(), 300)
    res.insert(2, (None, float(lon), float(lat)))  # entre os nós 2 e 3
    edits = set_landuse(ctx, GeometryEdits(), 300, res)
    farm = landuse_ring(ctx, edits, 301)
    farm.append((None, float(lon), float(lat)))  # entre os nós 3 e 2
    edits = set_landuse(ctx, edits, 301, farm)
    out = {e["id"]: e for e in apply_to_context(_context(), edits)["elements"]}
    new = [n for n in out[300]["nodes"] if n < 0]
    assert len(new) == 1 and new[0] in out[301]["nodes"]
    # Exportação: o mesmo nó novo nas duas ways.
    mid = (None, x, y)
    res_ring = [(1, -50.0, 50.0), (2, -50.0, 0.0), mid, (3, 50.0, 0.0), (4, 50.0, 50.0)]
    farm_ring = [(2, -50.0, 0.0), (5, -50.0, -50.0), (6, 50.0, -50.0), (3, 50.0, 0.0), mid]
    manual = {300: ([1, 2, 3, 4, 1], res_ring), 301: ([2, 5, 6, 3, 2], farm_ring)}
    plan = plan_edits([], _osm_xml(NODES, WAYS), CRS, RULES, manual=manual, keep_nodes={7})
    refs = {w: [int(nd.attrib["ref"]) for nd in el.findall("nd")] for w, el in plan.ways.items()}
    (shared,) = {r for r in refs[300] if r < 0}
    assert shared in refs[301]
    assert 7 in plan.nodes and "action" not in plan.nodes[7].attrib  # vai sem mudar


def test_new_building_uses_the_osm_node_it_was_connected_to(tmp_path: Path) -> None:
    from shapely.geometry import Polygon

    from osm3d.domain.models import Building
    from osm3d.export.osm_xml import write_josm_file

    plan = plan_edits([], _osm_xml(NODES, WAYS), CRS, RULES, keep_nodes={4})
    source = tmp_path / "fetch.osm"
    source.write_bytes(_osm_xml({}, {}))
    # Edifício novo com um canto no nó 4 (50, 50) do residencial.
    fp = Polygon([(40, 40), (50, 40), (50, 50), (40, 50)])
    b = Building(osm_type="new", osm_id=1, osm_version=0, footprint=fp)
    out = write_josm_file(
        [(b, {"building": "yes"})], source, tmp_path / "out.osm", crs=CRS, edits=plan
    )
    root = ET.parse(out).getroot()
    (way,) = [w for w in root.iter("way") if w.find("tag[@k='building']") is not None]
    refs = [int(nd.attrib["ref"]) for nd in way.findall("nd")]
    assert 4 in refs and len(set(refs)) == 4
    assert sum(n.attrib["id"] == "4" for n in root.iter("node")) == 1


def test_connected_nodes_of_new_buildings(tmp_path: Path) -> None:
    from shapely.geometry import Polygon

    from osm3d.domain.models import Building
    from osm3d.pipeline import _connected_nodes
    from tests.test_app import _app_with_candidate

    cfg = _app_with_candidate(tmp_path).state.ctx.cfg
    path = tmp_path / "ctx.json"
    path.write_text(json.dumps(_context()), encoding="utf-8")
    # Um canto no nó 4 (partilhado pelo residencial e pela via); os outros soltos.
    fp = Polygon([(40, 40), (50, 40), (50, 50), (40, 50)])
    b = Building(osm_type="new", osm_id=1, osm_version=0, footprint=fp)
    assert _connected_nodes(cfg, [b], path) == {4: [300, 400]}
    far = Building(
        osm_type="new", osm_id=2, osm_version=0, footprint=Polygon([(0, 0), (5, 0), (5, 5)])
    )
    assert _connected_nodes(cfg, [far], path) == {}


def test_review_and_wall_edits_reuse_cached_topology(tmp_path: Path) -> None:
    """Aceitar um edifício novo (só muda a decisão) e editar um muro não refazem a
    topologia nem o contexto OSM editado; o resultado é o mesmo que refazê-los."""
    from osm3d.app.service import Overrides
    from osm3d.geometry_edits import GeometryEdits
    from osm3d.pipeline import context_path
    from tests.test_app import _app_with_candidate

    app = _app_with_candidate(tmp_path)
    svc = app.state.ctx.svc
    context_path(svc.cfg).write_text(json.dumps(_context()), encoding="utf-8")
    key = "new/123456"

    def full() -> dict:  # type: ignore[type-arg]
        svc._topology_changed()
        return svc.building(key).model_dump()

    svc.building(key)
    topo = svc._topo
    svc.save_review(key, "accepted", "ok", Overrides(), None)
    assert svc._topo is topo  # não foi refeita
    fast = svc.building(key).model_dump()
    assert fast["review_status"] == "accepted" and fast == full()
    # Rejeitar tira-o da topologia: aí refaz-se.
    topo = svc._topo
    svc.save_review(key, "rejected", None, Overrides(), None)
    assert svc._topo is not topo
    svc.reset_review(key)
    assert svc.building(key).review_status == "pending"

    # Um landuse editado gera uma versão do contexto; editar só um muro não gera outra.
    d = svc.landuse_detail(300)
    nodes = d["nodes"]
    lon, lat = TO_WGS.transform(50, -2)
    nodes[2] = [3, lon, lat]
    svc.save_landuse(300, nodes)
    path = svc._effective_context()[1]
    walls = GeometryEdits(**{**svc.edits.__dict__, "walls": {"wall/1": [(0.0, 0.0), (1.0, 1.0)]}})
    assert walls.signature() != svc.edits.signature()
    assert walls.signature(walls=False, buildings=False) == svc.edits.signature(
        walls=False, buildings=False
    )
    svc._save_edits(walls)
    assert svc._effective_context()[1] == path


def test_osm_mode_previews_the_adjusted_landuse(tmp_path: Path) -> None:
    """No modo "ajustar o OSM", o detalhe e a pré-visualização trazem o landuse como fica
    na exportação (o mapa mostra-o logo); noutro modo, nada."""
    from osm3d.app.service import Overrides
    from osm3d.pipeline import context_path
    from tests.test_app import _app_with_candidate

    app = _app_with_candidate(tmp_path)
    svc = app.state.ctx.svc
    key = "new/123456"
    to_wgs = Transformer.from_crs(svc.cfg.processing_crs, "EPSG:4326", always_xy=True)
    minx, miny, maxx, maxy = svc.base[key].footprint.bounds
    # Residencial que contém o edifício todo menos uma lasca de 0,4 m a este.
    ring = [(minx - 20, miny - 20), (maxx - 0.4, miny - 20), (maxx - 0.4, maxy + 20),
            (minx - 20, maxy + 20), (minx - 20, miny - 20)]  # fmt: skip
    refs = [1, 2, 3, 4, 1]
    geom = [dict(zip(("lon", "lat"), to_wgs.transform(x, y), strict=True)) for x, y in ring]
    ctx = {"elements": [{"type": "way", "id": 500, "nodes": refs, "geometry": geom,
                         "tags": {"landuse": "residential"}}]}  # fmt: skip
    context_path(svc.cfg).write_text(json.dumps(ctx), encoding="utf-8")

    adjs = svc.detail(key)["osm_adjustments"]
    assert [a["id"] for a in adjs] == ["way/500"]
    assert adjs[0]["geometry"]["type"] == "Polygon" and "contornar" in adjs[0]["description"]
    # O landuse ajustado passa a conter o edifício.
    xs, ys = to_wgs.transform(maxx, (miny + maxy) / 2)
    from shapely.geometry import Point, shape

    assert shape(adjs[0]["geometry"]).contains(Point(xs, ys))

    assert svc.preview(key, Overrides(topology="keep"))["osm_adjustments"] == []
    assert svc.preview(key, Overrides(topology="osm"))["osm_adjustments"][0]["id"] == "way/500"
    # Um edifício do OSM não tem este modo.
    osm_key = next(k for k in svc.base if not k.startswith("new/"))
    assert svc.detail(osm_key)["osm_adjustments"] is None
