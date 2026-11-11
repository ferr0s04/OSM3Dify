from __future__ import annotations

import numpy as np
import pytest

from osm3d.domain.models import RoofShape
from osm3d.reconstruction.heights import height_from_points
from osm3d.reconstruction.roof_models import LocalFrame, roof_surface

A, B, Z0, H = 10.0, 5.0, 6.0, 3.0


def _z(shape: RoofShape, u: float, v: float) -> float:
    return float(roof_surface(shape, np.array([u]), np.array([v]), A, B, Z0, H)[0])


def test_flat() -> None:
    assert _z(RoofShape.FLAT, 3, 2) == pytest.approx(Z0)


def test_gabled_ridge_and_eaves() -> None:
    assert _z(RoofShape.GABLED, 7, 0) == pytest.approx(Z0 + H)
    assert _z(RoofShape.GABLED, 7, B) == pytest.approx(Z0)
    assert _z(RoofShape.GABLED, A, 0) == pytest.approx(Z0 + H)  # empena: sem quebra no topo


def test_hipped_ridge_shorter_than_building() -> None:
    assert _z(RoofShape.HIPPED, A - B, 0) == pytest.approx(Z0 + H)  # fim da cumeeira
    assert _z(RoofShape.HIPPED, A, 0) == pytest.approx(Z0)  # topo é água
    assert _z(RoofShape.HIPPED, 0, B) == pytest.approx(Z0)


def test_pyramidal_apex_at_centre() -> None:
    assert _z(RoofShape.PYRAMIDAL, 0, 0) == pytest.approx(Z0 + H)
    assert _z(RoofShape.PYRAMIDAL, A / 2, 0) == pytest.approx(Z0 + H / 2)


def test_skillion_monotonic_across() -> None:
    assert _z(RoofShape.SKILLION, 0, B) == pytest.approx(Z0)
    assert _z(RoofShape.SKILLION, 0, -B) == pytest.approx(Z0 + H)


def test_local_frame_axis() -> None:
    frame = LocalFrame(cx=100.0, cy=200.0, azimuth_deg=90.0, half_length=A, half_width=B)
    u, v = frame.to_local(np.array([105.0, 100.0]), np.array([200.0, 203.0]))
    np.testing.assert_allclose(u, [5.0, 0.0], atol=1e-9)  # este = ao longo do eixo
    np.testing.assert_allclose(np.abs(v), [0.0, 3.0], atol=1e-9)


def test_height_from_points() -> None:
    z = np.linspace(100.0, 110.0, 300)
    est = height_from_points(z, ground_z=95.0, percentile=95)
    assert est.value == pytest.approx(14.5)
    assert est.confidence == 1.0


def test_height_rejects_points_below_ground() -> None:
    with pytest.raises(ValueError):
        height_from_points(np.array([90.0, 91.0]), ground_z=95.0)
