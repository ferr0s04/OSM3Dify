from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
import rasterio
from rasterio.transform import from_origin
from shapely.geometry import box

from osm3d.domain.models import Building, QAFlag, RoofShape
from osm3d.evaluation.benchmark import evaluate, normalize_osm_shape
from osm3d.export.geopackage import read_buildings, write_buildings
from osm3d.geospatial.point_clouds import PointSet, assign_points, index_tiles
from osm3d.geospatial.raster import DtmMosaic, sample_ground_elevation
from osm3d.reconstruction.roof_fitting import fit_roof
from tests.conftest import make_part
from tests.synthetic import make_roof


def _building(
    osm_id: int, part_shape: RoofShape | None = RoofShape.GABLED, **tags: str
) -> Building:
    parts = [make_part(part_shape)] if part_shape else []
    return Building(
        osm_type="way",
        osm_id=osm_id,
        osm_version=2,
        footprint=box(0, 0, 20, 10),
        existing_tags={"building": "house", **tags},
        parts=parts,
        diagnostics={"rectangularity": 1.0, "n_points": 1500.0},
        qa_flags=[QAFlag(code="x", message="y", severity="info")],
    )


def test_geopackage_roundtrip(tmp_path: Path) -> None:
    buildings = [_building(1, **{"roof:shape": "gabled"}), _building(2, None)]
    path = write_buildings(buildings, tmp_path / "out.gpkg", "EPSG:3763")
    restored = read_buildings(path)
    assert [b.key for b in restored] == ["way/1", "way/2"]
    assert restored[0].parts[0].roof.shape.value is RoofShape.GABLED
    assert restored[0].footprint.equals(buildings[0].footprint)
    assert restored[0].review_status == "pending"
    # Escrever de novo substitui o ficheiro (não acrescenta).
    write_buildings(buildings[:1], path, "EPSG:3763")
    assert len(read_buildings(path)) == 1


def test_assign_points_with_offset() -> None:
    xyz = np.array([[1.0, 1.0, 5], [15.0, 5.0, 5], [25.0, 5.0, 5], [21.0, 1.0, 5]])
    pts = PointSet(xyz, np.full(4, 6, dtype=np.uint8))
    a, b = box(0, 0, 10, 10), box(10, 0, 20, 10)
    assert [i.tolist() for i in assign_points(pts, [a, b])] == [[0], [1]]
    # Footprints deslocados 2 m para este: o ponto x=21 passa a cair em b.
    assert [i.tolist() for i in assign_points(pts, [a, b], (2.0, 0.0))] == [[], [1, 3]]


def test_ground_sampling(tmp_path: Path) -> None:
    # MDT inclinado: cota = 50 + 0.1·x (x em metros a partir da origem).
    path = tmp_path / "MDT-50cm-155480-07-2025_v03.tif"
    res, n = 0.5, 200
    xs = np.arange(n) * res + res / 2
    data = np.tile(50 + 0.1 * xs, (n, 1)).astype("float32")
    with rasterio.open(
        path,
        "w",
        driver="GTiff",
        width=n,
        height=n,
        count=1,
        dtype="float32",
        crs="EPSG:3763",
        transform=from_origin(0, 100, res, res),
        nodata=-999,
    ) as ds:
        ds.write(data, 1)
    with DtmMosaic([path]) as dtm:
        ground = sample_ground_elevation(dtm, box(40, 40, 60, 60), buffer_m=2.0, percentile=5)
    # Anel de x≈37.5 a 62.5 → cota baixa perto de 50 + 0.1·38 ≈ 53.8
    assert ground == pytest.approx(53.9, abs=0.3)
    assert index_tiles(tmp_path, (".tif",)) == {"155480": path}


def test_fit_then_evaluate() -> None:
    syn = make_roof(RoofShape.HIPPED)
    fit = fit_roof(syn.footprint, syn.points, syn.ground_z)
    b = _building(1, None, **{"roof:shape": "hipped", "building:levels": "2"})
    from osm3d.domain.models import BuildingPart

    b = b.model_copy(
        update={
            "parts": [
                BuildingPart(id="p", geometry=syn.footprint, height=fit.height, roof=fit.roof)
            ]
        }
    )
    report = evaluate([b], min_shape_confidence=0.7)
    assert report.shapes.n_correct == 1
    assert report.heights.n == 1
    assert report.heights.storey_heights[0] == pytest.approx(3.0, abs=0.1)  # 6 m / 2 pisos


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("lean_to", "skillion"), ("Hipped", "hipped"), ("half-hipped", "outra"), (None, None)],
)
def test_normalize_osm_shape(raw: str | None, expected: str | None) -> None:
    assert normalize_osm_shape(raw) == expected
