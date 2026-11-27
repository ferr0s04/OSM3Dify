"""Contornos de edifícios editados no mapa da revisão (municipais e do OSM)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pyproj import Transformer
from shapely.geometry import Polygon

from osm3d.domain.models import Building, QAFlag, RoofShape
from osm3d.footprint_edits import (
    REFIT_FAILED,
    FootprintRefitter,
    apply_footprint_edits,
    fit_signature,
)
from osm3d.geospatial.point_clouds import PointSet
from tests.conftest import make_part

CRS = "EPSG:3763"
TO_WGS = Transformer.from_crs(CRS, "EPSG:4326", always_xy=True)


def _fake_refit(calls: list[str]):  # type: ignore[no-untyped-def]
    def refit(b: Building, fp: Polygon) -> Building:
        calls.append(b.key)
        part = make_part(RoofShape.FLAT).model_copy(update={"geometry": fp})
        flag = QAFlag(code="footprint_edited", message="reajustado", severity="info")
        return b.model_copy(update={"footprint": fp, "parts": [part], "qa_flags": [flag]})

    return refit


def _square(x: float = 0.0, y: float = 0.0, s: float = 10.0) -> Polygon:
    return Polygon([(x, y), (x + s, y), (x + s, y + s), (x, y + s)])


def _building() -> Building:
    fp = _square()
    return Building(
        osm_type="new",
        osm_id=1,
        osm_version=0,
        footprint=fp,
        parts=[make_part(RoofShape.GABLED).model_copy(update={"geometry": fp})],
        ground_elevation_m=10.0,
    )


def test_apply_footprint_edits_reuses_saved_fits() -> None:
    b = _building()
    edited = _square(s=12.0)
    calls: list[str] = []
    saved: dict[str, tuple[str, str]] = {}

    def save(key: str, sig: str, data: str) -> None:
        saved[key] = (sig, data)

    (out,) = apply_footprint_edits([b], {b.key: edited}, {}, _fake_refit(calls), save)
    assert calls == [b.key] and out.footprint.area == pytest.approx(144)
    assert saved[b.key][0] == fit_signature(b, edited)
    # O mesmo contorno: o reajuste guardado serve (sem voltar ao LiDAR).
    (again,) = apply_footprint_edits([b], {b.key: edited}, saved, _fake_refit(calls), save)
    assert calls == [b.key] and again.parts[0].roof.shape.value is RoofShape.FLAT
    # Outro contorno: reajusta de novo; sem edição, o edifício fica como estava.
    apply_footprint_edits([b], {b.key: _square(s=11.0)}, saved, _fake_refit(calls), save)
    assert calls == [b.key, b.key]
    assert apply_footprint_edits([b], {}, saved, _fake_refit(calls)) == [b]


def test_refit_without_dtm_is_flagged(tmp_path: Path) -> None:
    from osm3d.config import AreaConfig, Config, PathsConfig

    cfg = Config(
        area=AreaConfig(name="t", bbox=(-8.68, 41.27, -8.65, 41.29)),
        paths=PathsConfig(processed=tmp_path, output=tmp_path),
    )
    refit = FootprintRefitter(cfg, lambda bounds: PointSet.empty(), dict, list)
    out = refit(_building(), _square(s=12.0))
    assert out.footprint.area == pytest.approx(144) and not out.parts
    assert REFIT_FAILED in {f.code for f in out.qa_flags}


def _client(tmp_path: Path) -> tuple[TestClient, list[str]]:
    from tests.test_app import _app_with_candidate

    app = _app_with_candidate(tmp_path)
    client = TestClient(app)
    client.get("/api/buildings")  # cria o serviço
    calls: list[str] = []
    app.state.ctx.svc.refitter = _fake_refit(calls)
    return client, calls


def test_edit_municipal_building_outline(tmp_path: Path) -> None:
    client, calls = _client(tmp_path)
    d = client.get("/api/buildings/new/123456").json()
    g = d["geometry"]
    assert g["editable"] and g["kind"] == "new" and not g["edited"] and d["parts_editable"]
    nodes = g["nodes"]
    assert all(n[0] is None for n in nodes)
    area = d["area"]
    # Um vértice puxado 3 m para fora (no sentido do primeiro para o segundo).
    lon, lat = nodes[1][1], nodes[1][2]
    x, y = Transformer.from_crs("EPSG:4326", CRS, always_xy=True).transform(lon, lat)
    nodes[1] = [None, *TO_WGS.transform(x + 3, y)]
    r = client.put("/api/buildings/new/123456/geometry", json={"nodes": nodes})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["geometry"]["edited"] and d["edited"] and calls == ["new/123456"]
    assert d["area"] != area and "footprint_edited" in d["info_flags"]
    # Municipal: divide-se em partes também com o contorno editado (exporta-se do próprio
    # contorno); com a divisão gravada, o contorno já não se repõe.
    assert d["parts_editable"] and d["parts_reason"] is None
    base = "/api/buildings/new/123456"
    view = client.get(f"{base}/parts").json()
    xs, ys = [q[0] for q in view["outline"]], [q[1] for q in view["outline"]]
    mid = (min(xs) + max(xs)) / 2
    layout = client.post(f"{base}/parts/start").json()["layout"]
    split = client.post(
        f"{base}/parts/split",
        json={"layout": layout, "part": 0, "a": [mid, min(ys) - 1], "b": [mid, max(ys) + 1]},
    ).json()["layout"]
    parts = {"layout": split, "overrides": [{}, {}]}
    r = client.put(f"{base}/review", json={"status": "accepted", "parts": parts})
    assert r.status_code == 200, r.text
    assert r.json()["n_parts"] == 2
    assert client.delete(f"{base}/geometry").status_code == 422
    client.put(f"{base}/review", json={"status": "accepted"})
    # A exportação (CLI) usa o reajuste guardado, sem voltar ao LiDAR.
    from osm3d.pipeline import review_buildings, with_footprint_edits

    cfg = client.app.state.ctx.cfg  # type: ignore[attr-defined]
    out = {b.key: b for b in with_footprint_edits(cfg, review_buildings(cfg), _fake_refit(calls))}
    assert out["new/123456"].footprint.area == pytest.approx(d["area"], abs=1)
    assert calls == ["new/123456"]
    # Um anel que se cruza não é aceite; repor volta ao contorno municipal.
    bad = [nodes[0], nodes[2], nodes[1], nodes[3]]
    assert client.put("/api/buildings/new/123456/geometry", json={"nodes": bad}).status_code == 422
    d = client.delete("/api/buildings/new/123456/geometry").json()
    assert not d["geometry"]["edited"] and d["area"] == area and d["parts_editable"]


def _ll(x: float, y: float) -> dict[str, float]:
    lon, lat = TO_WGS.transform(x, y)
    return {"lon": float(lon), "lat": float(lat)}


def test_edit_osm_building_outline_moves_shared_nodes(tmp_path: Path) -> None:
    from osm3d.pipeline import context_path, manual_landuse

    client, calls = _client(tmp_path)
    cfg = client.app.state.ctx.cfg  # type: ignore[attr-defined]
    # Contexto: o edifício way/10 (nós 1–4, como no OSM) e um landuse colado nos nós 2 e 3.
    to_crs = Transformer.from_crs("EPSG:4326", CRS, always_xy=True)
    pos = {
        1: (-8.6700, 41.2800),
        2: (-8.66976, 41.2800),
        3: (-8.66976, 41.2801),
        4: (-8.6700, 41.2801),
    }
    xy = {k: to_crs.transform(*v) for k, v in pos.items()}
    xy[5] = (xy[2][0] + 30, xy[2][1])
    xy[6] = (xy[3][0] + 30, xy[3][1])

    def way(wid: int, refs: list[int], tags: dict[str, str]) -> dict:  # type: ignore[type-arg]
        geom = [_ll(*xy[r]) for r in refs]
        return {"type": "way", "id": wid, "nodes": refs, "geometry": geom, "tags": tags}

    ctx = {
        "elements": [
            way(10, [1, 2, 3, 4, 1], {"building": "house"}),
            way(300, [2, 5, 6, 3, 2], {"landuse": "farmland"}),
        ]
    }
    context_path(cfg).write_text(json.dumps(ctx), encoding="utf-8")
    d = client.get("/api/buildings/way/10").json()
    g = d["geometry"]
    assert g["editable"] and g["kind"] == "osm" and [n[0] for n in g["nodes"]] == [1, 2, 3, 4]
    assert set(g["shared"]) == {"2", "3"}
    # O nó 3 (partilhado) vai 2 m para leste: o landuse acompanha.
    nodes = g["nodes"]
    nodes[2] = [3, *TO_WGS.transform(xy[3][0] + 2, xy[3][1])]
    r = client.put("/api/buildings/way/10/geometry", json={"nodes": nodes})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["geometry"]["edited"] and calls == ["way/10"]
    assert client.get("/api/landuse/300").json()["edited"]
    assert set(manual_landuse(cfg)) == {10, 300}  # vão os dois no .osm da exportação
    # Clicado fora da revisão, o mesmo edifício abre-se como geometria (com a chave).
    assert client.get("/api/landuse/10").json()["building_key"] == "way/10"
    d = client.delete("/api/buildings/way/10/geometry").json()
    assert not d["geometry"]["edited"] and not client.get("/api/landuse/300").json()["edited"]


def test_outline_not_editable_with_parts_or_without_context(tmp_path: Path) -> None:
    client, _ = _client(tmp_path)
    # Sem contexto OSM, o contorno de um edifício do OSM não se edita aqui.
    g = client.get("/api/buildings/way/10").json()["geometry"]
    assert not g["editable"] and "contexto OSM" in g["reason"]
    # Dividido em partes (guardado): o contorno fica fixo.
    start = client.post("/api/buildings/new/123456/parts/start").json()["layout"]
    xs = [v[0] for v in start["vertices"]]
    ys = [v[1] for v in start["vertices"]]
    cx = (min(xs) + max(xs)) / 2
    split = {"layout": start, "part": 0, "a": [cx, min(ys) - 1], "b": [cx, max(ys) + 1]}
    r = client.post("/api/buildings/new/123456/parts/split", json=split)
    assert r.status_code == 200, r.text
    body = {"status": "pending", "parts": {"layout": r.json()["layout"], "overrides": []}}
    assert client.put("/api/buildings/new/123456/review", json=body).status_code == 200
    g = client.get("/api/buildings/new/123456").json()["geometry"]
    assert not g["editable"] and "partes" in g["reason"]
    ring = [[None, -8.6, 41.28], [None, -8.5999, 41.28], [None, -8.5999, 41.2801]]
    r = client.put("/api/buildings/new/123456/geometry", json={"nodes": ring})
    assert r.status_code == 422 and "partes" in r.json()["detail"]


def test_split_into_buildings_via_api(tmp_path: Path) -> None:
    client, _ = _client(tmp_path)
    url = "/api/buildings/new/123456"
    start = client.post(f"{url}/parts/start").json()["layout"]
    xs = [v[0] for v in start["vertices"]]
    ys = [v[1] for v in start["vertices"]]
    cx = (min(xs) + max(xs)) / 2
    body = {"layout": start | {"split": "buildings"}, "part": 0,
            "a": [cx, min(ys) - 1], "b": [cx, max(ys) + 1]}  # fmt: skip
    layout = client.post(f"{url}/parts/split", json=body).json()["layout"]
    assert layout["split"] == "buildings" and len(layout["parts"]) == 2  # o modo mantém-se
    prev = client.post(f"{url}/parts/preview", json={"layout": layout, "overrides": []}).json()
    assert prev["split"] == "buildings"
    first, second = (p["tags"] for p in prev["parts"])
    assert "building:part" not in first and "building:part" not in second
    assert second["building"] == "yes" and "height" in second
    assert all(p["estimated"]["height"] for p in prev["parts"])  # para preencher os campos
    review = {"status": "accepted", "parts": {"layout": layout, "overrides": []}}
    d = client.put(f"{url}/review", json=review).json()
    assert d["tags"]["split"] == "buildings" and len(d["tags"]["parts"]) == 2
    assert client.get(f"{url}/parts").json()["layout"]["split"] == "buildings"
