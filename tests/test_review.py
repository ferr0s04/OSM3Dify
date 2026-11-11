from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
from shapely.geometry import box

from osm3d.domain.models import Building, BuildingPart, RoofShape, Source
from osm3d.export.mesh import roof_mesh, roof_z, wall_mesh
from osm3d.export.s3db_tags import part_to_tags
from osm3d.reconstruction.roof_fitting import fit_roof
from osm3d.review import Overrides, Review, ReviewStore, apply_overrides, apply_review
from tests.synthetic import CX, CY, make_roof


@pytest.fixture
def fitted() -> Building:
    syn = make_roof(RoofShape.GABLED, length=20, width=10, azimuth_deg=90, roof_height=3.0)
    fit = fit_roof(syn.footprint, syn.points, syn.ground_z)
    part = BuildingPart(id="way/1#1", geometry=syn.footprint, height=fit.height, roof=fit.roof)
    return Building(
        osm_type="way",
        osm_id=1,
        osm_version=4,
        footprint=syn.footprint,
        ground_elevation_m=syn.ground_z,
        parts=[part],
    )


def test_fit_stores_exact_model(fitted: Building) -> None:
    m = fitted.parts[0].roof.model
    assert m is not None
    assert m.eave_z == pytest.approx(106.0, abs=0.1)
    assert m.roof_height == pytest.approx(3.0, abs=0.1)
    # A superfície regenerada reproduz o telhado: cumeeira no centro, beirais nos lados.
    z = roof_z(m, RoofShape.GABLED, np.array([CX, CX]), np.array([CY, CY + 4.99]), 0)
    assert z == pytest.approx([109.0, 106.0], abs=0.1)


def test_store_roundtrip(tmp_path: Path) -> None:
    store = ReviewStore(tmp_path / "r.sqlite")
    rev = Review(
        osm_key="way/1",
        osm_version=4,
        status="accepted",
        note="ok",
        overrides=Overrides(height=9.5),
    )
    store.save(rev)
    got = store.all()["way/1"]
    assert got.status == "accepted" and got.overrides.height == 9.5 and got.note == "ok"
    store.delete("way/1")
    assert store.all() == {}


def test_override_height_keeps_shape_and_moves_eaves(fitted: Building) -> None:
    part = apply_overrides(fitted, fitted.parts[0], Overrides(height=12.0))
    assert part.height.value == 12.0 and part.height.source is Source.MANUAL
    assert part.roof.shape.value is RoofShape.GABLED
    assert part.roof.shape.source is Source.LIDAR  # forma não foi corrigida
    assert part.roof.model is not None
    assert part.roof.model.eave_z == pytest.approx(100 + 12 - part.roof.model.roof_height)


def test_override_shape_and_orientation(fitted: Building) -> None:
    part = apply_overrides(
        fitted, fitted.parts[0], Overrides(roof_shape=RoofShape.HIPPED, roof_orientation="across")
    )
    assert part.roof.shape.value is RoofShape.HIPPED
    assert part.roof.shape.source is Source.MANUAL
    tags = part_to_tags(part)
    assert tags["roof:shape"] == "hipped" and tags["roof:orientation"] == "across"


def test_override_flat_and_skillion(fitted: Building) -> None:
    flat = apply_overrides(fitted, fitted.parts[0], Overrides(roof_shape=RoofShape.FLAT))
    assert flat.roof.height is None and "roof:height" not in part_to_tags(flat)
    sk = apply_overrides(
        fitted, fitted.parts[0], Overrides(roof_shape=RoofShape.SKILLION, roof_direction=90)
    )
    assert part_to_tags(sk)["roof:direction"] == "90"


def test_apply_review_and_stale(fitted: Building) -> None:
    rev = Review(osm_key="way/1", osm_version=4, status="rejected")
    assert apply_review(fitted, rev).review_status == "rejected"
    stale = Review(osm_key="way/1", osm_version=3, status="accepted")
    b = apply_review(fitted, stale)
    assert b.review_status == "pending"
    assert any(f.code == "review_stale" for f in b.qa_flags)


def test_mesh(fitted: Building) -> None:
    p = fitted.parts[0]
    roof = roof_mesh(fitted.footprint, p.roof.model, RoofShape.GABLED, 0.0)
    walls = wall_mesh(fitted.footprint, p.roof.model, RoofShape.GABLED, 100.0, 0.0)
    assert len(roof.faces) > 100 and roof.vertices[:, 2].max() == pytest.approx(109, abs=0.15)
    assert walls.vertices[:, 2].min() == pytest.approx(100.0)
    # Malha sem modelo (forma desconhecida): telhado plano à altura indicada.
    flat = roof_mesh(box(0, 0, 10, 10), None, RoofShape.UNKNOWN, 7.0)
    assert np.allclose(flat.vertices[:, 2], 7.0)
    js = roof.to_json((CX, CY, 100.0))
    assert len(js["vertices"]) == 3 * len(roof.vertices)
