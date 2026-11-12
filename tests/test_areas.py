"""Áreas de estudo (quadrículas, registo, área ativa) e processamento na aplicação."""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from shapely.geometry import Polygon, box, mapping

from osm3d.app.processing import PipelineJob
from osm3d.app.server import create_app
from osm3d.areas import AreaStore, area_store, next_id, with_active_area
from osm3d.config import AreaConfig, Config, PathsConfig, SourcesConfig
from osm3d.domain.models import Building, RoofShape
from osm3d.export.geopackage import write_buildings
from osm3d.geospatial.footprints import load_footprints
from osm3d.geospatial.tiling import TileKey, tiles_in_geometry
from osm3d.pipeline import (
    Stage,
    area_tiles,
    artefact_path,
    cache_matches,
    in_area,
    mark_cache,
    run_stage,
)
from tests.conftest import make_part
from tests.test_footprints import OSM_XML

# As duas quadrículas onde estão os edifícios de OSM_XML.
BASE = AreaConfig(name="area-1", label="Inicial", bbox=(-8.68, 41.27, -8.65, 41.29))
OTHER_TILES = ["150490", "151490"]


def make_cfg(tmp_path: Path) -> Config:
    return Config(
        area=BASE,
        paths=PathsConfig(processed=tmp_path / "processed", output=tmp_path / "out"),
        sources=SourcesConfig(
            lidar_dir=tmp_path / "lidar",
            dtm_dir=tmp_path / "dtm",
            dgt_tile_index=tmp_path / "sem-grelha.gpkg",
        ),
    )


def write_results(cfg: Config) -> None:
    """Resultados mínimos (fases fetch e qa) para a área de ``cfg``."""
    fetch = artefact_path(cfg, Stage.FETCH)
    fetch.parent.mkdir(parents=True, exist_ok=True)
    fetch.write_text(OSM_XML, encoding="utf-8")
    buildings = [
        b.model_copy(
            update={
                "parts": [make_part(RoofShape.FLAT).model_copy(update={"geometry": b.footprint})],
                "ground_elevation_m": 10.0,
                "osm_parts": [],
            }
        )
        for b in load_footprints(fetch, cfg.processing_crs, 20)
    ]
    write_buildings(buildings, artefact_path(cfg, Stage.QA), cfg.processing_crs)


# ------------------------------------------------------------------ modelo


def test_area_is_a_set_of_tiles() -> None:
    a = AreaConfig(name="x", tiles=[156480, "155480", "155480"])  # do YAML vêm números
    assert a.tiles == ["155480", "156480"]
    assert a.polygon().area == pytest.approx(2e6)
    w, s, e, n = a.bbox
    assert -8.70 < w < e < -8.64 and 41.27 < s < n < 41.29
    with pytest.raises(ValidationError):
        AreaConfig(name="x", tiles=[])
    with pytest.raises(ValidationError):
        AreaConfig(name="x", tiles=["12345"])
    with pytest.raises(ValidationError):
        AreaConfig(name="../fora", tiles=["155480"])


def test_legacy_bbox_becomes_tiles() -> None:
    # Configurações antigas (bbox + relação) passam às quadrículas da bbox.
    old = AreaConfig.model_validate(
        {"name": "v", "bbox": [-8.69911, 41.27348, -8.63541, 41.29553], "osm_relation_id": 1}
    )
    assert len(old.tiles) == 18 and "155480" in old.tiles


def test_buildings_and_lidar_follow_the_tiles(tmp_path: Path) -> None:
    cfg = make_cfg(tmp_path).model_copy(update={"area": AreaConfig(name="um", tiles=["155480"])})
    x0, y0, x1, y1 = TileKey.from_dgt_name("155480").bounds

    def at(x: float, y: float, i: int) -> Building:
        return Building(
            osm_type="way", osm_id=i, osm_version=1, footprint=box(x, y, x + 10, y + 10)
        )

    inside = at(x0 + 100, y0 + 100, 1)
    crossing_in = at(x1 - 6, y0 + 100, 2)  # passa o limite, centróide dentro: entra
    crossing_out = at(x1 - 4, y0 + 200, 3)  # passa o limite, centróide fora: é da vizinha
    outside = at(x1 + 100, y0 + 100, 4)  # quadrícula vizinha
    got = in_area(cfg, [inside, crossing_in, crossing_out, outside])
    assert [b.osm_id for b in got] == [1, 2]

    for name in ("155480", "156480"):
        p = cfg.sources.lidar_dir / f"LO-{name}-07-2025_v05.laz"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"x")
    assert list(area_tiles(cfg, cfg.sources.lidar_dir, (".laz",))) == ["155480"]


def test_tiles_in_boundary_polygon() -> None:
    # Polígono a cobrir parte de duas quadrículas.
    from pyproj import Transformer

    a, b = TileKey.from_dgt_name("155480").bounds, TileKey.from_dgt_name("156480").bounds
    t = Transformer.from_crs("EPSG:3763", "EPSG:4326", always_xy=True)
    poly = box(a[0] + 500, a[1] + 200, b[0] + 300, a[3] - 200)
    xs, ys = poly.exterior.xy
    lon, lat = t.transform(list(xs), list(ys))
    names = {k.dgt_name for k in tiles_in_geometry(Polygon(zip(lon, lat, strict=True)))}
    assert names == {"155480", "156480"}


def test_osm_cache_follows_the_tiles(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = make_cfg(tmp_path)
    fetched: list[bool] = []

    def fake_fetch(bbox: Any, url: str, out: Path, *a: Any, force: bool = False, **kw: Any) -> Path:
        fetched.append(force)
        if force or not out.exists():
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text("<osm/>", encoding="utf-8")
        return out

    monkeypatch.setattr("osm3d.acquisition.osm.fetch_buildings", fake_fetch)
    out = artefact_path(cfg, Stage.FETCH)
    out.parent.mkdir(parents=True)
    out.write_text("<osm/>", encoding="utf-8")  # cache antiga, sem registo das quadrículas
    assert not cache_matches(out, cfg.area.tiles)
    run_stage(cfg, Stage.FETCH)
    assert fetched == [True] and cache_matches(out, cfg.area.tiles)
    run_stage(cfg, Stage.FETCH)  # mesmas quadrículas: usa a cache
    assert fetched == [True, False]
    other = cfg.model_copy(update={"area": BASE.model_copy(update={"tiles": OTHER_TILES})})
    run_stage(other, Stage.FETCH)  # quadrículas mudaram: obtém de novo
    assert fetched == [True, False, True]
    mark_cache(out, ["155480"])
    assert not cache_matches(out, OTHER_TILES)


# ------------------------------------------------------------------ registo


def test_next_id() -> None:
    assert next_id(set()) == "area-1"
    assert next_id({"area-1", "area-3", "outra"}) == "area-4"


def test_store(tmp_path: Path) -> None:
    store = AreaStore(tmp_path / "areas.json", BASE)
    assert list(store.areas()) == ["area-1"] and store.active() == BASE
    other = store.create(OTHER_TILES, "Outra")
    assert other.name == "area-2" and other.label == "Outra"
    first_change = store.tiles_changed_at("area-2")
    assert list(store.areas()) == ["area-1", "area-2"] and first_change is not None
    assert store.create(["152490"]).name == "area-3"  # sem nome: só o id
    store.activate("area-2")
    assert store.active() == other
    # Mudar só o nome a mostrar não conta como alteração das quadrículas.
    store.save(other.model_copy(update={"label": "Outra 2"}))
    assert store.tiles_changed_at("area-2") == first_change
    with pytest.raises(ValueError):
        store.delete("area-2")  # ativa
    with pytest.raises(ValueError):
        store.delete("area-1")  # do config.yaml
    with pytest.raises(KeyError):
        store.activate("nao-existe")
    # A área do config.yaml pode ser ajustada (a versão registada prevalece).
    store.save(BASE.model_copy(update={"tiles": ["155480"]}), activate=True)
    assert store.active().tiles == ["155480"]
    store.delete("area-2")
    assert list(store.areas()) == ["area-1", "area-3"]


def test_migrates_place_named_ids(tmp_path: Path) -> None:
    # Registo antigo: a área inicial "vilar-e-mosteiro" alterada para outra quadrícula
    # (e processada), com o nome herdado. O config.yaml passou a ter id area-1.
    processed = tmp_path / "processed"
    (processed / "vilar-e-mosteiro").mkdir(parents=True)
    (processed / "vilar-e-mosteiro" / "06_qa.gpkg").write_bytes(b"x")
    old = {
        "active": None,
        "areas": {
            "vilar-e-mosteiro": {
                "name": "vilar-e-mosteiro",
                "label": "Inicial",  # = nome da área inicial: herdado
                "tiles": ["156482"],
                "updated_at": "2026-11-12T11:07:28+00:00",
            },
            "vila-do-conde": {"name": "vila-do-conde", "label": "VdC", "tiles": ["150490"]},
        },
    }
    path = tmp_path / "areas.json"
    path.write_text(json.dumps(old), encoding="utf-8")
    store = AreaStore(path, BASE, processed)
    areas = store.areas()
    assert list(areas) == ["area-1", "area-2", "area-3"]
    assert areas["area-2"].tiles == ["156482"] and areas["area-2"].label is None
    assert areas["area-3"].label == "VdC"  # nome próprio: fica
    assert (processed / "area-2" / "06_qa.gpkg").exists()
    assert not (processed / "vilar-e-mosteiro").exists()
    assert store.active().name == "area-2"  # era a área em uso
    assert json.loads(path.read_text(encoding="utf-8"))["version"] == 2
    assert list(AreaStore(path, BASE, processed).areas()) == list(areas)  # só uma vez


def test_with_active_area(tmp_path: Path) -> None:
    cfg = make_cfg(tmp_path)
    assert cfg.paths.areas_path == tmp_path / "areas.json"
    area_store(cfg).create(OTHER_TILES, activate=True)
    active = with_active_area(cfg)
    assert active.area.name == "area-2" and cfg.area.name == "area-1"
    assert artefact_path(active, Stage.QA).parent == tmp_path / "processed" / "area-2"


# ------------------------------------------------------------------------ job


def test_pipeline_job_runs_stages_and_reports_errors(tmp_path: Path) -> None:
    cfg = make_cfg(tmp_path)
    calls: list[tuple[str, bool]] = []
    done: list[bool] = []

    def runner(c: Config, stage: Stage, *, force: bool, progress: Any) -> Path:
        calls.append((stage.value, force))
        progress(f"  {stage.value} ok")
        if stage is Stage.ROOFS and c.area.name == "falha":
            raise FileNotFoundError("sem LAZ")
        return Path(f"{stage.value}.out")

    job = PipelineJob(on_done=lambda c, ok: done.append(ok), runner=runner)
    job.start(cfg, redo_from=Stage.FETCH)
    assert job.wait(10)
    s = job.state()
    assert s["ok"] and [x["status"] for x in s["stages"]] == ["done"] * 7
    assert calls[0] == ("fetch", True) and ("prepare", False) in calls
    assert done == [True] and "Processamento concluído." in s["log"]

    calls.clear()
    job.start(cfg.model_copy(update={"area": BASE.model_copy(update={"name": "falha"})}))
    assert job.wait(10)
    s = job.state()
    assert not s["ok"] and "sem LAZ" in s["error"]
    status = {x["stage"]: x["status"] for x in s["stages"]}
    assert status["roofs"] == "error" and status["qa"] == "skipped"
    assert done == [True, False]


def test_pipeline_job_only_runs_missing_stages(tmp_path: Path) -> None:
    cfg = make_cfg(tmp_path)
    ran: list[tuple[str, bool]] = []
    states = {"fetch": "done", "prepare": "done", "roofs": "stale", "colours": "done",
              "qa": "done", "candidates": "done"}  # fmt: skip

    def runner(c: Config, stage: Stage, *, force: bool, progress: Any) -> Path:
        ran.append((stage.value, force))
        return Path("x")

    def state_of(c: Config, stage: Stage) -> tuple[str, str]:
        return states.get(stage.value, "done"), ""

    job = PipelineJob(runner=runner, state_of=state_of)  # type: ignore[arg-type]
    job.start(cfg, candidates=True)
    assert job.wait(10)
    # roofs desatualizada → corre, e as seguintes da cadeia também; candidatos feitos.
    assert [s for s, _ in ran] == ["roofs", "colours", "qa"]
    status = {s["stage"]: s["status"] for s in job.state()["stages"]}
    assert status["fetch"] == status["prepare"] == status["candidates"] == "cached"

    # Tudo feito: nada corre. Refazer a partir de telhados: roofs e seguintes, sem
    # voltar ao Overpass; a partir de fetch: o OSM é pedido de novo (force).
    states["roofs"] = "done"
    ran.clear()
    job.start(cfg)
    assert job.wait(10) and ran == []
    job.start(cfg, redo_from=Stage.ROOFS)
    assert job.wait(10)
    assert ran == [("roofs", False), ("colours", False), ("qa", False)]
    ran.clear()
    job.start(cfg, candidates=True, redo_from=Stage.FETCH)
    assert job.wait(10)
    assert ran[0] == ("fetch", True) and ran[-1] == ("candidates", True)
    with pytest.raises(ValueError):
        job.start(cfg, redo_from=Stage.CANDIDATES)  # candidatos não incluídos


def test_stage_state_follows_files(tmp_path: Path) -> None:
    import os

    from osm3d.pipeline import stage_state

    cfg = make_cfg(tmp_path)
    assert stage_state(cfg, Stage.PREPARE) == ("missing", "ainda não processada")
    fetch = artefact_path(cfg, Stage.FETCH)
    fetch.parent.mkdir(parents=True)
    fetch.write_text("<osm/>", encoding="utf-8")
    assert stage_state(cfg, Stage.FETCH)[0] == "stale"  # sem registo das quadrículas
    mark_cache(fetch, cfg.area.tiles)
    assert stage_state(cfg, Stage.FETCH)[0] == "done"
    prepare = artefact_path(cfg, Stage.PREPARE)
    prepare.write_bytes(b"x")
    assert stage_state(cfg, Stage.PREPARE)[0] == "done"
    os.utime(fetch, (prepare.stat().st_mtime + 10,) * 2)  # OSM obtido depois
    assert stage_state(cfg, Stage.PREPARE) == ("stale", "a fase 'fetch' é mais recente")
    assert stage_state(cfg, Stage.HEIGHTS)[0] == "done"  # fase de passagem
    # LAZ mais recente do que os telhados → telhados desatualizados.
    roofs = artefact_path(cfg, Stage.ROOFS)
    roofs.write_bytes(b"x")
    os.utime(prepare, (roofs.stat().st_mtime - 10,) * 2)
    laz = cfg.sources.lidar_dir / f"LO-{cfg.area.tiles[0]}-07-2025_v05.laz"
    laz.parent.mkdir(parents=True)
    laz.write_bytes(b"x")
    os.utime(laz, (roofs.stat().st_mtime + 10,) * 2)
    assert stage_state(cfg, Stage.ROOFS) == ("stale", "há LAZ/MDT mais recentes")


def test_pipeline_job_cancel_between_stages(tmp_path: Path) -> None:
    gate = threading.Event()

    def runner(c: Config, stage: Stage, *, force: bool, progress: Any) -> Path:
        gate.wait(5)
        return Path("x")

    job = PipelineJob(runner=runner)
    job.start(make_cfg(tmp_path), candidates=True)
    with pytest.raises(RuntimeError):
        job.start(make_cfg(tmp_path))  # uma corrida de cada vez
    job.cancel()
    gate.set()
    assert job.wait(10)
    s = job.state()
    assert s["ok"] is False and s["stages"][0]["status"] == "done"
    assert {x["status"] for x in s["stages"][1:]} == {"cancelled"}
    assert s["stages"][-1]["stage"] == "candidates"


# ------------------------------------------------------------------------ API


def lookup(relation_id: int) -> tuple[str | None, Any]:
    # Limite fictício dentro das duas quadrículas de OTHER_TILES.
    from pyproj import Transformer

    a = TileKey.from_dgt_name(OTHER_TILES[0]).bounds
    b = TileKey.from_dgt_name(OTHER_TILES[1]).bounds
    t = Transformer.from_crs("EPSG:3763", "EPSG:4326", always_xy=True)
    (w, e), (s, n) = t.transform([a[0] + 100, b[2] - 100], [a[1] + 100, a[3] - 100])
    return "Freguesia Teste", mapping(box(w, s, e, n))


@pytest.fixture
def ctx_client(tmp_path: Path) -> tuple[TestClient, Config, threading.Event]:
    cfg = make_cfg(tmp_path)
    write_results(cfg)
    gate = threading.Event()
    gate.set()

    def runner(c: Config, stage: Stage, *, force: bool, progress: Any) -> Path:
        gate.wait(5)
        if stage is Stage.QA:
            write_results(c)
        return artefact_path(c, stage)

    app = create_app(cfg, pipeline=PipelineJob(runner=runner), relation_lookup=lookup)
    return TestClient(app), cfg, gate


def test_areas_api_create_activate_process(
    ctx_client: tuple[TestClient, Config, threading.Event],
) -> None:
    client, cfg, gate = ctx_client
    a = client.get("/api/areas").json()
    assert a["active"] == "area-1" and a["review_available"]
    generation = a["review_generation"]
    (base,) = a["areas"]
    assert base["processed"] and base["from_config"] and not base["stale"]
    assert base["tiles"] == BASE.tiles and len(base["bbox"]) == 4
    assert client.get("/api/summary").json()["area"] == "area-1"

    rel = client.get("/api/osm/relation/42").json()
    assert rel["label"] == "Freguesia Teste" and rel["tiles"] == OTHER_TILES
    body = {"label": rel["label"], "tiles": rel["tiles"], "activate": True}
    a = client.post("/api/areas", json=body).json()
    # Id gerado e neutro; o nome do limite é só a etiqueta.
    assert a["active"] == "area-2" and not a["review_available"]
    assert a["review_generation"] > generation  # o frontend sabe que tem de recarregar
    new = next(x for x in a["areas"] if x["name"] == "area-2")
    assert new["label"] == "Freguesia Teste"
    # Área nova sem resultados: revisão indisponível, dados e áreas continuam.
    assert client.get("/api/summary").status_code == 503
    st = client.get("/api/data/status").json()
    assert st["area"]["name"] == "area-2" and st["area"]["tiles"] == OTHER_TILES
    # Uma segunda área com as mesmas quadrículas é outra área (outro id).
    unnamed = client.post("/api/areas", json={"tiles": OTHER_TILES}).json()
    assert [x["name"] for x in unnamed["areas"]] == ["area-1", "area-2", "area-3"]

    # Processar a área ativa → no fim a revisão passa a usar os seus resultados.
    gate.clear()
    client.post("/api/processing", json={"candidates": False})
    assert client.post("/api/areas/area-1/activate").status_code == 409  # em curso
    assert client.post("/api/processing", json={}).status_code == 409
    gate.set()
    assert client.app.state.ctx.pipeline.wait(10)  # type: ignore[attr-defined]
    p = client.get("/api/processing").json()
    assert p["ok"] and p["area"] == "area-2"
    assert client.get("/api/summary").json()["area"] == "area-2"
    assert (cfg.paths.processed / "area-2" / "06_qa.gpkg").exists()
    assert client.get("/api/areas").json()["review_generation"] > a["review_generation"]

    # Alterar as quadrículas da área ativa: os resultados ficam desatualizados.
    upd = {"label": "Freg.", "tiles": [*OTHER_TILES, "152490"]}
    a = client.put("/api/areas/area-2", json=upd).json()
    cur = next(x for x in a["areas"] if x["name"] == "area-2")
    assert len(cur["tiles"]) == 3 and cur["processed"] and cur["stale"]
    assert cur["label"] == "Freg."
    assert client.put("/api/areas/nao-existe", json=upd).status_code == 404
    assert client.put("/api/areas/area-2", json={"tiles": ["x"]}).status_code == 422
    assert client.put("/api/areas/area-2", json={"tiles": []}).status_code == 422
    assert client.delete("/api/areas/area-2").status_code == 422  # ativa
    assert client.post("/api/areas/area-1/activate").json()["active"] == "area-1"
    assert client.get("/api/summary").json()["area"] == "area-1"
    assert client.delete("/api/areas/area-2").status_code == 200
    assert client.delete("/api/areas/area-1").status_code == 422
