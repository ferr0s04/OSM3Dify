from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np
import pytest
from shapely.geometry import box

import osm3d.reconstruction.candidates as cand_mod
from osm3d.acquisition.municipal import (
    MunicipalBuilding,
    fetch_municipal_buildings,
    load_municipal_buildings,
    parse_features,
)
from osm3d.config import MunicipalSourceConfig
from osm3d.domain.models import Building
from osm3d.geospatial.point_clouds import PointSet
from osm3d.reconstruction.candidates import _TileContext, candidate_id, merge_municipal

FEATURE_SET = """<?xml version="1.0" encoding="UTF-8"?><FeatureSet><Features>
<Feature><Property><Name>gid</Name><Value>79282</Value></Property>
<Property><Name>geom</Name><Value>MULTIPOLYGON (((0 0, 10 0, 10 8, 0 8, 0 0)))</Value></Property>
<Property><Name>descr</Name><Value>Vivenda Casa</Value></Property>
<Property><Name>objeto</Name><Value>const_poligonos</Value></Property></Feature>
<Feature><Property><Name>gid</Name><Value>111792</Value></Property>
<Property><Name>geom</Name><Value>MULTIPOLYGON (((10 0, 14 0, 14 4, 10 4, 10 0)))</Value></Property>
<Property><Name>descr</Name><Value>Arrecadação_Anexo</Value></Property></Feature>
<Feature><Property><Name>gid</Name><Value>5</Value></Property></Feature>
</Features></FeatureSet>""".encode()


def test_parse_mapguide_feature_set() -> None:
    out = parse_features(FEATURE_SET)
    assert [(b.gid, b.descr, b.building_tag) for b in out] == [
        (79282, "Vivenda Casa", "house"),
        (111792, "Arrecadação_Anexo", "shed"),
    ]
    assert out[0].geometry.area == pytest.approx(80.0)
    assert MunicipalBuilding(1, box(0, 0, 1, 1), "Piscinas").building_tag is None


class _FakeClient:
    def __init__(self) -> None:
        self.boxes: list[tuple[float, float, float, float]] = []

    def select(self, b: tuple[float, float, float, float]) -> bytes:
        self.boxes.append(b)
        return FEATURE_SET


def test_fetch_caches_geojson_and_dedupes(tmp_path: Path) -> None:
    cfg = MunicipalSourceConfig(enabled=True, chunk_m=250, delay_s=0.2)
    client = _FakeClient()
    out = tmp_path / "m.geojson"
    cfg_fast = cfg.model_copy(update={"delay_s": 0.2})
    fetch_municipal_buildings((0, 0, 300, 200), cfg_fast, out, client=client)  # type: ignore[arg-type]
    assert len(client.boxes) == 2  # 2 × 1 caixas de 250 m
    loaded = load_municipal_buildings(out)
    assert sorted(b.gid for b in loaded) == [79282, 111792]  # mesmos gid nas 2 caixas
    # Em cache: não volta a pedir.
    fetch_municipal_buildings((0, 0, 300, 200), cfg_fast, out, client=client)  # type: ignore[arg-type]
    assert len(client.boxes) == 2


def _ctx(points_xy: np.ndarray, z: float, osm: list[Any] | None = None) -> _TileContext:
    xyz = np.column_stack([points_xy, np.full(len(points_xy), z)])
    pts = PointSet(xyz, np.full(len(xyz), 6, np.uint8))
    return _TileContext(
        "000000",
        (-100, -100, 100, 100),
        (-130, -130, 130, 130),
        pts,
        None,
        osm or [],
        None,  # type: ignore[arg-type]
    )


def _grid(poly: Any, step: float = 0.3) -> np.ndarray:
    x0, y0, x1, y1 = poly.bounds
    xs, ys = np.meshgrid(np.arange(x0 + 0.1, x1, step), np.arange(y0 + 0.1, y1, step))
    return np.column_stack([xs.ravel(), ys.ravel()])


def _lidar_candidate(poly: Any) -> Building:
    c = poly.centroid
    return Building(osm_type="new", osm_id=candidate_id(c.x, c.y), osm_version=0, footprint=poly)


def test_merge_municipal(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cand_mod, "sample_ground_elevation", lambda *a, **k: 0.0)
    house, shed = box(0, 0, 10, 8), box(10, 0, 14, 4)
    gone = box(40, 40, 50, 50)  # demolido: sem pontos LiDAR
    pool = box(60, 0, 70, 5)
    in_osm = box(-30, 0, -20, 10)
    municipal = [
        MunicipalBuilding(1, house, "Vivenda Casa"),
        MunicipalBuilding(2, shed, "Arrecadação_Anexo"),
        MunicipalBuilding(3, gone, "Vivenda Casa"),
        MunicipalBuilding(4, pool, "Piscinas"),
        MunicipalBuilding(5, in_osm, "Vivenda Casa"),
    ]
    recent = box(20, 20, 28, 28)  # só no LiDAR: construção recente
    pts = np.vstack([_grid(house), _grid(shed), _grid(recent), _grid(in_osm)])
    ctx = _ctx(pts, 5.0, osm=[in_osm])
    # Candidato LiDAR que junta casa + anexo (encostados): substituído pelos municipais.
    lidar = [_lidar_candidate(box(0, 0, 14, 8)), _lidar_candidate(recent)]
    cfg = MunicipalSourceConfig(enabled=True, source_tag="CM Teste")
    out, stats = merge_municipal(ctx, lidar, municipal, (0.0, 0.0), cfg)
    assert stats.from_municipal == 2 and stats.without_lidar == 1
    assert stats.replaced == 1 and stats.not_in_municipal == 1
    tags = sorted(b.existing_tags["building"] for b in out if "municipal_gid" in b.diagnostics)
    assert tags == ["house", "shed"]
    muni = next(b for b in out if b.diagnostics.get("municipal_gid") == 1.0)
    assert muni.existing_tags["source"] == "CM Teste"
    assert muni.footprint.equals(house)
    only_lidar = next(b for b in out if "municipal_gid" not in b.diagnostics)
    assert "not_in_municipal" in {f.code for f in only_lidar.qa_flags}


def test_merge_municipal_needs_height(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cand_mod, "sample_ground_elevation", lambda *a, **k: 4.5)
    house = box(0, 0, 10, 8)
    ctx = _ctx(_grid(house), 5.0)  # 0,5 m acima do terreno: muro/sebe, não edifício
    out, stats = merge_municipal(
        ctx, [], [MunicipalBuilding(1, house, "Vivenda Casa")], (0.0, 0.0), MunicipalSourceConfig()
    )
    assert out == [] and stats.without_lidar == 1
