from __future__ import annotations

import numpy as np
import pytest
from shapely.geometry import Polygon, box
from shapely.geometry.polygon import orient

from osm3d.domain.models import RoofModel, RoofShape
from osm3d.export.mesh import roof_mesh
from osm3d.geospatial.point_clouds import PointSet
from osm3d.reconstruction.roof_fitting import fit_roof
from osm3d.reconstruction.skeleton import (
    gable_edges,
    renders_as_skeleton,
    skeleton_basis,
    wavefront_time,
)

# L: duas alas de 8 m de largura (meia-largura 4 m), canto reentrante em (8, 8).
L = Polygon([(0, 0), (20, 0), (20, 8), (8, 8), (8, 20), (0, 20)])
# Planta "dobrada" como no exemplo do F4 Map: dois troços de 9 m de largura a 30°.
BENT = Polygon([(0, 0), (16, 0), (25.8, 5.7), (21.3, 13.5), (14.0, 9), (0, 9)])


def _pt(*xy: float) -> tuple[np.ndarray, np.ndarray]:
    return np.array([xy[0]]), np.array([xy[1]])


def test_wavefront_matches_straight_skeleton() -> None:
    # Dentro de uma ala, a altura é a distância à parede mais próxima.
    assert wavefront_time(L, *_pt(15, 2))[0] == pytest.approx(2.0, abs=0.01)
    assert wavefront_time(L, *_pt(15, 4))[0] == pytest.approx(4.0, abs=0.01)  # cumeeira
    # Vale do canto reentrante: distância às duas linhas (e não ao vértice).
    assert wavefront_time(L, *_pt(5.5, 5.5))[0] == pytest.approx(2.5, abs=0.05)
    assert wavefront_time(L, *_pt(30, 30))[0] == 0.0


def test_gable_edges_are_wing_tips() -> None:
    poly = orient(L, 1.0)
    coords = np.asarray(poly.exterior.coords)[:-1]
    tips = {
        tuple(sorted(map(tuple, (coords[i], coords[(i + 1) % len(coords)]))))
        for i in gable_edges(poly, 4.0)
    }
    assert tips == {((0.0, 20.0), (8.0, 20.0)), ((20.0, 0.0), (20.0, 8.0))}


def test_gabled_skeleton_has_vertical_gable_ends() -> None:
    # Na ponta da ala, a meio: gabled vai à cumeeira, hipped desce ao beiral.
    x, y = _pt(19.99, 4)
    assert skeleton_basis(L, RoofShape.GABLED, x, y)[0] == pytest.approx(1.0, abs=0.02)
    assert skeleton_basis(L, RoofShape.HIPPED, x, y)[0] < 0.05
    # Cumeeira contínua ao longo da dobra (meio da ala e meio do canto).
    for p in ((10, 4), (4, 10), (4, 4)):
        assert skeleton_basis(L, RoofShape.GABLED, *_pt(*p))[0] == pytest.approx(1.0, abs=0.03)


def test_renders_as_skeleton_only_for_complex_footprints() -> None:
    assert renders_as_skeleton(L, RoofShape.GABLED)
    assert renders_as_skeleton(BENT, RoofShape.HIPPED)
    assert not renders_as_skeleton(box(0, 0, 20, 10), RoofShape.GABLED)
    assert not renders_as_skeleton(L, RoofShape.SKILLION)


@pytest.mark.parametrize("footprint", [L, BENT], ids=["L", "dobrado"])
@pytest.mark.parametrize("shape", [RoofShape.GABLED, RoofShape.HIPPED])
def test_fit_recognises_skeleton_roofs(footprint: Polygon, shape: RoofShape) -> None:
    rng = np.random.default_rng(1)
    minx, miny, maxx, maxy = footprint.bounds
    n = int(10 * footprint.area * 1.6)
    x, y = rng.uniform(minx, maxx, n), rng.uniform(miny, maxy, n)
    from shapely import contains_xy

    inside = contains_xy(footprint, x, y)
    x, y = x[inside], y[inside]
    z = 106 + 3.0 * skeleton_basis(footprint, shape, x, y) + rng.normal(0, 0.04, len(x))
    pts = PointSet(np.column_stack([x, y, z]), np.full(len(x), 6, dtype=np.uint8))
    fit = fit_roof(footprint, pts, 100.0)
    assert fit.roof.shape.value is shape
    assert fit.roof.shape.confidence > 0.9
    assert fit.roof.model is not None and fit.roof.model.skeleton
    assert fit.height.value == pytest.approx(9.0, abs=0.15)


def test_mesh_follows_the_bend() -> None:
    model = RoofModel(
        cx=0,
        cy=0,
        azimuth_deg=90,
        half_length=10,
        half_width=4,
        eave_z=6.0,
        roof_height=3.0,
        skeleton=True,
    )
    mesh = roof_mesh(BENT, model, RoofShape.GABLED, 0.0, resolution_m=0.25)
    assert mesh.vertices[:, 2].max() == pytest.approx(9.0, abs=0.05)
    # Vértices perto do eixo de ambos os troços estão à cota da cumeeira.
    v = mesh.vertices
    near_axis_1 = (np.abs(v[:, 1] - 4.5) < 0.2) & (v[:, 0] > 3) & (v[:, 0] < 10)
    assert v[near_axis_1, 2].max() == pytest.approx(9.0, abs=0.1)
