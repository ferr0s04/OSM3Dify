from __future__ import annotations

import numpy as np
import pytest

from osm3d.domain.models import BuildingPart, RoofShape
from osm3d.export.s3db_tags import part_to_tags
from osm3d.geometry import axis_difference_deg, direction_difference_deg
from osm3d.geospatial.point_clouds import PointSet
from osm3d.reconstruction.roof_fitting import FitParams, fit_roof
from tests.synthetic import make_roof

AZIMUTHS = [0.0, 37.0, 128.0]


def _check_common(fit, syn, shape: RoofShape) -> None:  # type: ignore[no-untyped-def]
    assert fit.roof.shape.value is shape
    assert fit.roof.shape.confidence >= 0.9
    assert fit.height.value == pytest.approx(syn.total_height, abs=0.15)
    if shape is not RoofShape.FLAT:
        assert fit.roof.height is not None
        assert fit.roof.height.value == pytest.approx(syn.roof_height, abs=0.15)


@pytest.mark.parametrize("azimuth", AZIMUTHS)
@pytest.mark.parametrize("across", [False, True])
def test_gabled(azimuth: float, across: bool) -> None:
    syn = make_roof(RoofShape.GABLED, azimuth_deg=azimuth, across=across)
    fit = fit_roof(syn.footprint, syn.points, syn.ground_z)
    _check_common(fit, syn, RoofShape.GABLED)
    expected_ridge = azimuth + (90.0 if across else 0.0)
    assert axis_difference_deg(fit.roof.ridge_azimuth_deg, expected_ridge) < 1.0


@pytest.mark.parametrize("azimuth", AZIMUTHS)
def test_hipped(azimuth: float) -> None:
    syn = make_roof(RoofShape.HIPPED, azimuth_deg=azimuth)
    fit = fit_roof(syn.footprint, syn.points, syn.ground_z)
    _check_common(fit, syn, RoofShape.HIPPED)
    assert axis_difference_deg(fit.roof.ridge_azimuth_deg, azimuth) < 1.0


@pytest.mark.parametrize("azimuth", AZIMUTHS)
def test_pyramidal(azimuth: float) -> None:
    syn = make_roof(RoofShape.PYRAMIDAL, length=10, width=10, azimuth_deg=azimuth)
    fit = fit_roof(syn.footprint, syn.points, syn.ground_z)
    _check_common(fit, syn, RoofShape.PYRAMIDAL)


@pytest.mark.parametrize("azimuth", AZIMUTHS)
@pytest.mark.parametrize(("across", "flip"), [(False, False), (False, True), (True, False)])
def test_skillion(azimuth: float, across: bool, flip: bool) -> None:
    syn = make_roof(
        RoofShape.SKILLION, azimuth_deg=azimuth, across=across, flip=flip, roof_height=1.5
    )
    fit = fit_roof(syn.footprint, syn.points, syn.ground_z)
    _check_common(fit, syn, RoofShape.SKILLION)
    # Água virada para +v do referencial do modelo (v = 90° à esquerda de u).
    # O referencial rodado normaliza o eixo u para [0, 180), tal como o gerador.
    model_az = (azimuth + 90.0) % 180.0 if across else azimuth
    expected = model_az - 90.0 + (180.0 if flip else 0.0)
    assert direction_difference_deg(fit.roof.facing_azimuth_deg, expected) < 2.0


def test_flat() -> None:
    syn = make_roof(RoofShape.FLAT, roof_height=0.0)
    fit = fit_roof(syn.footprint, syn.points, syn.ground_z)
    _check_common(fit, syn, RoofShape.FLAT)
    assert fit.roof.height is None


def test_very_low_slope_is_flat() -> None:
    # 0,2 m em 4,5 m de meia-largura ≈ 2,5° < 5°.
    syn = make_roof(RoofShape.GABLED, roof_height=0.2)
    fit = fit_roof(syn.footprint, syn.points, syn.ground_z)
    assert fit.roof.shape.value is RoofShape.FLAT


@pytest.mark.parametrize("shape", [RoofShape.GABLED, RoofShape.HIPPED])
def test_robust_to_vegetation_and_chimney(shape: RoofShape) -> None:
    syn = make_roof(shape, azimuth_deg=37, vegetation_fraction=0.15, chimney=True, seed=3)
    fit = fit_roof(syn.footprint, syn.points, syn.ground_z)
    _check_common(fit, syn, shape)


def test_low_density_still_works() -> None:
    syn = make_roof(RoofShape.HIPPED, density=3.0, noise_m=0.08, seed=5)
    fit = fit_roof(syn.footprint, syn.points, syn.ground_z)
    assert fit.roof.shape.value is RoofShape.HIPPED


def test_too_few_points_is_unknown() -> None:
    syn = make_roof(RoofShape.GABLED, density=0.1)
    fit = fit_roof(syn.footprint, syn.points, syn.ground_z, FitParams(min_points=30))
    assert fit.roof.shape.value is RoofShape.UNKNOWN
    assert fit.height.confidence < 0.5


def test_noise_is_unknown() -> None:
    syn = make_roof(RoofShape.FLAT)
    rng = np.random.default_rng(1)
    xyz = syn.points.xyz.copy()
    xyz[:, 2] = 106 + rng.uniform(0, 4, len(xyz))
    fit = fit_roof(syn.footprint, PointSet(xyz, syn.points.classification), syn.ground_z)
    assert fit.roof.shape.value is RoofShape.UNKNOWN


def test_no_points_raises() -> None:
    syn = make_roof(RoofShape.FLAT)
    empty = PointSet(np.empty((0, 3)), np.empty(0, dtype=np.uint8))
    with pytest.raises(ValueError):
        fit_roof(syn.footprint, empty, syn.ground_z)


def test_end_to_end_tags_gabled_across() -> None:
    syn = make_roof(RoofShape.GABLED, azimuth_deg=37, across=True, roof_height=3.2)
    fit = fit_roof(syn.footprint, syn.points, syn.ground_z)
    part = BuildingPart(id="p1", geometry=syn.footprint, height=fit.height, roof=fit.roof)
    tags = part_to_tags(part)
    assert tags == {
        "height": "9",  # 6 m até ao beiral + 3,2 m de telhado → 9,2 → arredonda a 9
        "roof:shape": "gabled",
        "roof:height": "3",
        "roof:orientation": "across",
    }
