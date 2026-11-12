"""Gestão de dados raw: grelha DGT, inventário local, downloads e limpeza."""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from osm3d.acquisition.dgt import DgtAsset, DgtCancelled, DgtItem, filename_for
from osm3d.acquisition.inventory import TileIndex, delete_local, scan_local
from osm3d.app.downloads import DownloadManager
from osm3d.app.server import create_app
from osm3d.config import AreaConfig, Config, PathsConfig, SourcesConfig
from osm3d.geospatial.tiling import TileKey, tile_rings_wgs84

AREA_BBOX = (-8.69911, 41.27348, -8.63541, 41.29553)  # 18 quadrículas


def make_cfg(tmp_path: Path, index: Path | None = None) -> Config:
    raw = tmp_path / "raw"
    return Config(
        area=AreaConfig(name="teste", bbox=AREA_BBOX),
        paths=PathsConfig(raw=raw, processed=tmp_path / "processed", output=tmp_path / "out"),
        sources=SourcesConfig(
            lidar_dir=raw / "lidar",
            dtm_dir=raw / "dtm",
            dsm_dir=raw / "dsm",
            orthophoto_dir=raw / "orthophotos",
            dgt_tile_index=index or raw / "dgt" / "inexistente.gpkg",
        ),
    )


def touch(path: Path, size: int = 10) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)
    return path


def write_index(path: Path, rows: list[tuple[str, int, str]]) -> Path:
    import geopandas as gpd
    from shapely.geometry import box

    geoms = [box(*TileKey.from_dgt_name(n).bounds) for n, _, _ in rows]
    gdf = gpd.GeoDataFrame(
        {
            "NAME": [r[0] for r in rows],
            "LOTE": [r[1] for r in rows],
            "DISPONIVEL": [r[2] for r in rows],
        },
        geometry=geoms,
        crs="EPSG:3763",
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    gdf.to_file(path, layer="tiles", driver="GPKG")
    return path


class FakeClient:
    """Imita o DgtClient: LAZ e MDT para as quadrículas em ``available``."""

    def __init__(self, available: set[str], block: threading.Event | None = None) -> None:
        self.available = available
        self.block = block
        self.searches: list[list[str]] = []

    def search(self, bbox: tuple[float, ...], collections: list[str]) -> list[DgtItem]:
        self.searches.append(collections)
        items = []
        for name in self.available:
            w, s, e, n = TileKey.from_dgt_name(name).bounds_wgs84()
            if w < bbox[2] and bbox[0] < e and s < bbox[3] and bbox[1] < n:  # vizinhas também
                coll = collections[0]
                prefix = "LO" if coll == "LAZ" else coll
                mt = "application/vnd.laszip" if coll == "LAZ" else "image/tiff"
                items.append(
                    DgtItem(f"{prefix}-{name}-07-2025", coll, name, 2, (DgtAsset("h", mt),))
                )
        return items

    def download(
        self,
        item: DgtItem,
        asset: DgtAsset,
        dest_dir: Path,
        *,
        on_bytes: Any = None,
        cancel: threading.Event | None = None,
    ) -> Path:
        if self.block is not None:
            while not (cancel and cancel.is_set()):
                if self.block.wait(0.01):
                    break
            if cancel and cancel.is_set():
                raise DgtCancelled(item.id)
        if on_bytes:
            on_bytes(5, 10)
        return touch(dest_dir / filename_for(item, asset), 1000)


# ----------------------------------------------------------------- unidades


def test_tile_rings_wgs84() -> None:
    (ring,) = tile_rings_wgs84([TileKey.from_dgt_name("155480")])
    assert len(ring) == 5 and ring[0] == ring[-1]
    w, s, e, n = TileKey.from_dgt_name("155480").bounds_wgs84()
    assert all(w - 1e-5 <= x <= e + 1e-5 and s - 1e-5 <= y <= n + 1e-5 for x, y in ring)


def test_tile_index(tmp_path: Path) -> None:
    path = write_index(tmp_path / "grid.gpkg", [("155480", 1, "nao"), ("156480", 2, "sim")])
    index = TileIndex(path)
    assert index.available and len(index) == 2
    assert index.get("155480").lote == 1 and index.get("156480").disponivel == "sim"
    assert index.get("999999").lote is None and "999999" not in index
    names = {t.name for t in index.in_bbox(AREA_BBOX)}
    assert names == {"155480", "156480"}  # só as que existem na grelha
    # Sem grelha: toda a malha de 1 km da bbox.
    assert len(TileIndex(tmp_path / "nada.gpkg").in_bbox(AREA_BBOX)) == 18


def test_scan_and_delete(tmp_path: Path) -> None:
    cfg = make_cfg(tmp_path)
    s = cfg.sources
    laz = touch(s.lidar_dir / "LO-155480-07-2025_v05.laz", 100)
    touch(s.lidar_dir / "LO-155480-07-2025_v04.laz", 90)  # versão antiga
    touch(s.dtm_dir / "MDT-50cm-155480-07-2025_v03.tif", 20)
    touch(s.lidar_dir / "LO-156480-07-2025_v05.laz.part", 50)  # interrompido
    keep = touch(s.lidar_dir / "LO-157480-07-2025_v05.laz", 100)
    other = touch(s.lidar_dir / "notas.txt")
    cache = touch(cfg.paths.processed / "teste" / "cache" / f"{laz.stem}.npz", 30)

    files = scan_local(s)
    assert {(f.tile, f.product, f.partial) for f in files} == {
        ("155480", "lidar", False),
        ("155480", "dtm", False),
        ("156480", "lidar", True),
        ("157480", "lidar", False),
    }
    assert sum(f.tile == "155480" and f.product == "lidar" for f in files) == 2

    removed = delete_local(s, cfg.paths.processed, ["155480", "156480"], ["lidar"])
    assert {f.path.name for f in removed} == {
        "LO-155480-07-2025_v05.laz",
        "LO-155480-07-2025_v04.laz",
        "LO-156480-07-2025_v05.laz.part",
        f"{laz.stem}.npz",
    }
    assert not cache.exists()
    assert (s.dtm_dir / "MDT-50cm-155480-07-2025_v03.tif").exists()  # produto não pedido
    assert keep.exists() and other.exists()
    with pytest.raises(ValueError):
        delete_local(s, cfg.paths.processed, ["155480"], ["tudo"])


def test_download_manager(tmp_path: Path) -> None:
    cfg = make_cfg(tmp_path)
    touch(cfg.sources.dtm_dir / "MDT-50cm-155480-07-2025_v01.tif")  # já existe
    changes: list[int] = []
    fake = FakeClient({"155480", "156480"})
    mgr = DownloadManager(cfg, lambda: fake, on_change=lambda: changes.append(1))
    mgr.submit(["155480", "156480", "157480"], ["lidar", "dtm"])
    assert mgr.wait(10)
    by = {(t["tile"], t["product"]): t for t in mgr.state()["tasks"]}
    assert by[("155480", "dtm")]["status"] == "exists"
    assert by[("155480", "lidar")]["status"] == "done"
    assert by[("155480", "lidar")]["files"] == ["LO-155480-07-2025_v02.laz"]
    # A pesquisa devolve também a vizinha: só a própria quadrícula é descarregada.
    assert by[("156480", "dtm")]["files"] == ["MDT-50cm-156480-07-2025_v02.tif"]
    assert by[("157480", "lidar")]["status"] == "missing"
    assert len(changes) == 3 and mgr.state()["version"] == 3
    assert len(list(cfg.sources.lidar_dir.iterdir())) == 2
    with pytest.raises(ValueError):
        mgr.submit(["12345"], ["lidar"])
    with pytest.raises(ValueError):
        mgr.submit(["155480"], ["foo"])


def test_download_manager_cancel_and_login_error(tmp_path: Path) -> None:
    cfg = make_cfg(tmp_path)
    block = threading.Event()
    mgr = DownloadManager(cfg, lambda: FakeClient({"155480", "156480"}, block))
    mgr.submit(["155480", "156480"], ["lidar"])
    assert "155480" in mgr.busy_tiles()
    mgr.cancel()
    assert mgr.wait(10)
    assert {t["status"] for t in mgr.state()["tasks"]} == {"cancelled"}
    assert not list(cfg.sources.lidar_dir.glob("*.laz"))

    def no_login() -> FakeClient:
        raise RuntimeError("Variáveis em falta (definir em .env): DGT_USER")

    mgr = DownloadManager(cfg, no_login)
    mgr.submit(["155480", "156480"], ["lidar"])
    assert mgr.wait(10)
    tasks = mgr.state()["tasks"]
    assert {t["status"] for t in tasks} == {"error"} and "DGT_USER" in tasks[0]["message"]


def write_sheet(path: Path, tiles_x: tuple[str, str]) -> Path:
    """GeoTIFF pequeno (EPSG:3763) com a extensão de um retângulo de quadrículas."""
    import numpy as np
    import rasterio
    from rasterio.transform import from_bounds

    a, b = TileKey.from_dgt_name(tiles_x[0]).bounds, TileKey.from_dgt_name(tiles_x[1]).bounds
    x0, y0, x1, y1 = min(a[0], b[0]), min(a[1], b[1]), max(a[2], b[2]), max(a[3], b[3])
    path.parent.mkdir(parents=True, exist_ok=True)
    with rasterio.open(
        path,
        "w",
        driver="GTiff",
        width=40,
        height=20,
        count=1,
        dtype="uint8",
        crs="EPSG:3763",
        transform=from_bounds(x0, y0, x1, y1, 40, 20),
    ) as dst:
        dst.write(np.zeros((1, 20, 40), dtype="uint8"))
    return path


def test_tile_items_keeps_sheet_products() -> None:
    from osm3d.acquisition.dgt import tile_items

    class Search:
        def search(self, bbox: Any, collections: list[str]) -> list[DgtItem]:
            return [
                DgtItem("LO-156480-07-2025", "LAZ", "156480", 5),  # vizinha: fora
                DgtItem("LO-155480-07-2025", "LAZ", "155480", 5),
                DgtItem("ORTOS-2025-cog-25cm-097-3", "ORTOS-2025", None, 1),  # folha: fica
            ]

    got = tile_items(Search(), TileKey.from_dgt_name("155480"), "x")
    assert sorted(i.id for i in got) == ["LO-155480-07-2025", "ORTOS-2025-cog-25cm-097-3"]


def test_orthophoto_sheet_covers_several_tiles(tmp_path: Path) -> None:
    from osm3d.acquisition.inventory import total_size

    cfg = make_cfg(tmp_path)
    # Folha de 3 × 2 km: quadrículas 154479..156480 (x 154–156, y 479–480).
    sheet = write_sheet(
        cfg.sources.orthophoto_dir / "ORTOS-2025-cog-25cm-097-3_v01.tif", ("154479", "156480")
    )
    files = [f for f in scan_local(cfg.sources) if f.product == "orthophoto"]
    assert {f.tile for f in files} == {"154479", "155479", "156479", "154480", "155480", "156480"}
    assert {f.shared for f in files} == {6} and total_size(files) == sheet.stat().st_size

    # Apagar uma quadrícula apaga a folha (e o inventário das outras).
    removed = delete_local(cfg.sources, cfg.paths.processed, ["155480"], ["orthophoto"])
    assert [f.path for f in removed] == [sheet] and not sheet.exists()


def test_manager_downloads_a_shared_sheet_once(tmp_path: Path) -> None:
    cfg = make_cfg(tmp_path)

    class SheetClient(FakeClient):
        downloads = 0

        def search(self, bbox: tuple[float, ...], collections: list[str]) -> list[DgtItem]:
            return [
                DgtItem(
                    "ORTOS-2025-cog-25cm-097-3",
                    collections[0],
                    None,
                    1,
                    (DgtAsset("h", "image/tiff"),),
                )
            ]

        def download(self, item: DgtItem, asset: DgtAsset, dest_dir: Path, **kw: Any) -> Path:
            SheetClient.downloads += 1
            return write_sheet(dest_dir / filename_for(item, asset), ("155480", "156480"))

    mgr = DownloadManager(cfg, lambda: SheetClient(set()))
    mgr.submit(["155480", "156480"], ["orthophoto"])
    assert mgr.wait(10)
    by = {t["tile"]: t for t in mgr.state()["tasks"]}
    assert by["155480"]["status"] == "done" and by["156480"]["status"] == "exists"
    assert SheetClient.downloads == 1


# ---------------------------------------------------------------------- API


@pytest.fixture
def data_client(tmp_path: Path) -> tuple[TestClient, Config]:
    index = write_index(
        tmp_path / "raw" / "dgt" / "grid.gpkg", [("155480", 1, "nao"), ("156480", 1, "sim")]
    )
    cfg = make_cfg(tmp_path, index)
    mgr = DownloadManager(cfg, lambda: FakeClient({"155480", "156480"}))
    return TestClient(create_app(cfg, downloads=mgr)), cfg


def test_app_without_results_serves_data(data_client: tuple[TestClient, Config]) -> None:
    client, _ = data_client
    r = client.get("/api/summary")
    assert r.status_code == 503 and "Sem resultados" in r.json()["detail"]
    assert client.get("/").status_code == 200
    st = client.get("/api/data/status").json()
    assert st["index"] == {"available": True, "path": st["index"]["path"], "n_tiles": 2}
    assert len(st["area"]["tiles"]) == 18 and "155480" in st["area"]["tiles"]
    assert st["totals"] == {"tiles": 0, "bytes": 0}
    assert [p["id"] for p in st["products"]] == ["lidar", "dtm", "dsm", "orthophoto"]


def test_data_api_download_and_delete(data_client: tuple[TestClient, Config]) -> None:
    client, cfg = data_client
    fc = client.get("/api/data/tiles", params={"bbox": ",".join(map(str, AREA_BBOX))}).json()
    feats = {f["properties"]["name"]: f for f in fc["features"]}
    assert set(feats) == {"155480", "156480"}
    assert feats["155480"]["id"] == 155480 and feats["155480"]["properties"]["state"] == "none"
    assert feats["155480"]["properties"]["lote"] == 1
    assert client.get("/api/data/tiles", params={"bbox": "-10,36,-6,42.2"}).status_code == 422
    assert client.get("/api/data/tiles", params={"bbox": "1,2,3"}).status_code == 422

    r = client.post("/api/data/downloads", json={"tiles": ["155480"], "products": ["lidar", "dtm"]})
    assert r.status_code == 200
    assert client.app.state.data.downloads.wait(10)  # type: ignore[attr-defined]
    state = client.get("/api/data/downloads").json()
    assert state["counts"] == {"done": 2} and not state["running"]

    named = client.get("/api/data/tiles", params={"names": "155480"}).json()["features"][0]
    props = named["properties"]
    assert props["state"] == "complete" and set(props["local"]) == {"lidar", "dtm"}
    st = client.get("/api/data/status").json()
    assert st["totals"]["tiles"] == 1 and st["local"]["features"][0]["properties"]["size"] == 2000

    # Só o MDT: o LAZ fica.
    r = client.post("/api/data/delete", json={"tiles": ["155480"], "products": ["dtm"]}).json()
    assert r["n_files"] == 1 and r["freed_bytes"] == 1000
    named = client.get("/api/data/tiles", params={"names": "155480"}).json()["features"][0]
    assert named["properties"]["state"] == "partial"
    # Por omissão, todos os produtos.
    r = client.post("/api/data/delete", json={"tiles": ["155480"]}).json()
    assert r["n_files"] == 1 and not list(cfg.sources.lidar_dir.iterdir())
    bad = client.post("/api/data/delete", json={"tiles": ["abc"]})
    assert bad.status_code == 422
    assert client.post("/api/data/downloads", json={"tiles": []}).status_code == 422


def test_delete_refused_while_downloading(tmp_path: Path) -> None:
    cfg = make_cfg(tmp_path)
    block = threading.Event()
    mgr = DownloadManager(cfg, lambda: FakeClient({"155480"}, block))
    client = TestClient(create_app(cfg, downloads=mgr))
    client.post("/api/data/downloads", json={"tiles": ["155480"], "products": ["lidar"]})
    r = client.post("/api/data/delete", json={"tiles": ["155480"]})
    assert r.status_code == 409 and "cancelar" in r.json()["detail"]
    client.delete("/api/data/downloads")
    assert mgr.wait(10)
