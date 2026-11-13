"""Formas raras (round, side_hipped) e hipped assimétrico: casos da revisão de Vila do Conde."""

from __future__ import annotations

import numpy as np
import shapely
from shapely.geometry import Polygon

from osm3d.domain.models import RoofShape
from osm3d.export.mesh import roof_mesh, wall_mesh
from osm3d.geometry import remove_spikes
from osm3d.geospatial.point_clouds import PointSet
from osm3d.reconstruction.roof_fitting import fit_roof
from osm3d.reconstruction.roof_models import LocalFrame, roof_surface
from tests.synthetic import CX, CY


def _points(frame: LocalFrame, z_of, density: float = 10.0, seed: int = 0):  # type: ignore[no-untyped-def]
    rng = np.random.default_rng(seed)
    a, b = frame.half_length, frame.half_width
    n = int(density * 4 * a * b)
    u, v = rng.uniform(-a, a, n), rng.uniform(-b, b, n)
    x, y = frame.to_world(u, v)
    z = z_of(u, v) + rng.normal(0, 0.04, n)
    cu, cv = np.array([-1, 1, 1, -1]) * a, np.array([-1, -1, 1, 1]) * b
    footprint = Polygon(np.column_stack(frame.to_world(cu, cv)))
    return footprint, PointSet(np.column_stack([x, y, z]), np.full(n, 6, dtype=np.uint8))


def test_hipped_with_one_short_end_is_hipped() -> None:
    # 16 × 9 m; uma ponta com água de 1 m, a outra de 4,5 m (casos corrigidos à mão:
    # o modelo hipped simétrico perdia para o side_hipped).
    frame = LocalFrame(CX, CY, 20.0, 8.0, 4.5)
    hip_pos, hip_neg = 1.0, 4.5
    r = 8.0 - (hip_pos + hip_neg) / 2
    shift = (hip_neg - hip_pos) / 2 / 8.0
    fp, pts = _points(
        frame,
        lambda u, v: roof_surface(
            RoofShape.HIPPED, u, v, 8.0, 4.5, 106.0, 3.0, ridge_ratio=r / 8.0, ridge_shift=shift
        ),
    )
    fit = fit_roof(fp, pts, 100.0)
    assert fit.roof.shape.value is RoofShape.HIPPED
    assert fit.roof.model is not None and fit.roof.model.ridge_shift


def test_ridge_shift_surface() -> None:
    u = np.array([-8.0, -4.0, 0.0, 6.0, 8.0])
    v = np.zeros(5)
    z = roof_surface(RoofShape.HIPPED, u, v, 8.0, 4.0, 0.0, 1.0, ridge_ratio=0.5, ridge_shift=0.25)
    # Cumeeira de u = -2 a u = 6: ponta −u com 6 m de água, ponta +u sem água (limite).
    assert z[0] == 0.0 and z[2] == 1.0 and z[3] == 1.0
    assert z[1] == np.float64(4.0 / 6.0)


def test_flat_roof_with_low_strip_is_not_round() -> None:
    # Telhado plano com uma faixa 0,4 m mais baixa ao longo de um dos lados (anexo,
    # caleira): o "round" explicava a queda só desse lado.
    frame = LocalFrame(CX, CY, 80.0, 6.0, 3.5)

    def z(u: np.ndarray, v: np.ndarray) -> np.ndarray:
        return np.where(v > 2.5, 105.6, 106.0)

    fp, pts = _points(frame, z)
    fit = fit_roof(fp, pts, 100.0)
    assert fit.roof.shape.value is not RoofShape.ROUND


def test_asymmetric_gabled_is_not_round() -> None:
    # Duas águas com beirais a cotas diferentes (um lado com anexo mais baixo).
    frame = LocalFrame(CX, CY, 0.0, 6.0, 4.5)

    def z(u: np.ndarray, v: np.ndarray) -> np.ndarray:
        return np.where(v > 0, 106.0 + 2.0 * (1 - v / 4.5), 104.5 + 3.5 * (1 + v / 4.5))

    fp, pts = _points(frame, z)
    assert fit_roof(fp, pts, 100.0).roof.shape.value is not RoofShape.ROUND


def test_remove_spikes() -> None:
    # Contorno com idas-e-voltas (de recortes): "válido", mas parte a malha 3D.
    spiky = Polygon(
        [(0, 0), (6, 0), (0, 0), (0.5, -0.5), (10, -0.5), (8, -0.5), (8, 10), (0, 10), (0, 0)]
    )
    clean = remove_spikes(spiky)
    assert clean.is_valid and clean.area == spiky.area
    assert len(clean.exterior.coords) < len(spiky.exterior.coords)
    assert isinstance(shapely.segmentize(clean, 0.5), Polygon)
    square = shapely.box(0, 0, 4, 4)
    assert remove_spikes(square).equals(square)


def test_mesh_survives_spiky_outline() -> None:
    spiky = Polygon(
        [(0, 0), (6, 0), (0, 0), (0.5, -0.5), (10, -0.5), (8, -0.5), (8, 10), (0, 10), (0, 0)]
    )
    roof = roof_mesh(spiky, None, RoofShape.FLAT, 10.0)
    walls = wall_mesh(spiky, None, RoofShape.FLAT, 0.0, 10.0)
    assert len(roof.faces) and len(walls.faces)
