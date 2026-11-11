from __future__ import annotations

import numpy as np
from shapely.geometry import box

from osm3d.domain.models import RoofShape
from osm3d.geometry import direction_difference_deg
from osm3d.geospatial.point_clouds import PointSet
from osm3d.reconstruction.roof_fitting import fit_main_volume, fit_roof
from tests.synthetic import CX, CY, make_roof


def _with_annex(shape: RoofShape, flip: bool = False) -> tuple[object, PointSet]:
    """Caso real way/330892659: telhado principal 18 x 9 m + alpendre plano de 3 m,
    1,5 m abaixo do beiral, ao longo de um dos lados maiores, no mesmo contorno."""
    main = make_roof(shape, length=18, width=9, azimuth_deg=90, roof_height=2.5, flip=flip, seed=2)
    rng = np.random.default_rng(5)
    n = int(10 * 18 * 3)
    annex = np.column_stack(
        [
            rng.uniform(CX - 9, CX + 9, n),
            rng.uniform(CY - 7.5, CY - 4.5, n),  # faixa a sul do volume principal
            np.full(n, main.eave_z - 1.5) + rng.normal(0, 0.04, n),
        ]
    )
    pts = PointSet(
        np.vstack([main.points.xyz, annex]),
        np.full(len(main.points) + n, 6, dtype=np.uint8),
    )
    footprint = box(CX - 9, CY - 7.5, CX + 9, CY + 4.5)
    return footprint, pts


def test_whole_footprint_fails_but_main_volume_is_found() -> None:
    footprint, pts = _with_annex(RoofShape.SIDE_HIPPED)
    whole = fit_roof(footprint, pts, 100.0)
    assert whole.roof.shape.value is RoofShape.UNKNOWN or whole.roof.shape.confidence < 0.7

    main = fit_main_volume(footprint, pts, 100.0)
    assert main is not None
    assert main.fit.roof.shape.value is RoofShape.SIDE_HIPPED
    assert 0.6 < main.fraction < 0.85  # ~9/12 da planta
    assert abs(main.fit.height.value - 8.5) < 0.2  # topo do volume principal
    assert main.fit.roof.facing_azimuth_deg is not None
    assert direction_difference_deg(main.fit.roof.facing_azimuth_deg, 90.0) < 3


def test_church_like_side_annex_and_tower_end() -> None:
    """Caso real way/330367625 (igreja): nave gabled 20 x 9 m, capelas laterais 1,2 m
    abaixo do beiral a sul e uma torre 2,5 m acima da cumeeira na ponta este."""
    nave = make_roof(RoofShape.GABLED, length=20, width=9, azimuth_deg=90, roof_height=3, seed=8)
    rng = np.random.default_rng(9)
    side = np.column_stack(
        [
            rng.uniform(CX - 10, CX + 10, 600),
            rng.uniform(CY - 8, CY - 4.5, 600),
            np.full(600, nave.eave_z - 1.2) + rng.normal(0, 0.04, 600),
        ]
    )
    tower = np.column_stack(
        [
            rng.uniform(CX + 10, CX + 14, 400),
            rng.uniform(CY - 4.5, CY + 4.5, 400),
            np.full(400, nave.eave_z + 3 + 2.5) + rng.normal(0, 0.04, 400),
        ]
    )
    xyz = np.vstack([nave.points.xyz, side, tower])
    pts = PointSet(xyz, np.full(len(xyz), 6, dtype=np.uint8))
    footprint = box(CX - 10, CY - 8, CX + 14, CY + 4.5)
    main = fit_main_volume(footprint, pts, 100.0)
    assert main is not None
    assert main.fit.roof.shape.value is RoofShape.GABLED
    assert abs(main.fit.height.value - 9.0) < 0.3  # nave: 6 m + 3 m (sem a torre)


def test_asymmetric_gabled_is_gabled() -> None:
    from osm3d.reconstruction.roof_models import roof_surface

    syn = make_roof(RoofShape.FLAT, length=16, width=10, azimuth_deg=0, seed=1)
    x, y = syn.points.xyz[:, 0], syn.points.xyz[:, 1]
    # Cumeeira deslocada 25 % da meia-largura (beirais de largura diferente).
    z = roof_surface(RoofShape.GABLED, y - CY, -(x - CX), 8, 5, 106, 3, ridge_offset=0.25)
    pts = PointSet(np.column_stack([x, y, z]), syn.points.classification)
    fit = fit_roof(syn.footprint, pts, 100.0, free_ridge=True)
    assert fit.roof.shape.value is RoofShape.GABLED and fit.roof.shape.confidence > 0.9
    assert fit.roof.model is not None and abs(fit.roof.model.ridge_offset or 0) > 0.1
    # Sem free_ridge (planta inteira), a cumeeira fica a meio.
    centred = fit_roof(syn.footprint, pts, 100.0)
    assert centred.roof.model is None or not centred.roof.model.ridge_offset


def test_main_volume_gabled_and_none_when_homogeneous() -> None:
    footprint, pts = _with_annex(RoofShape.GABLED)
    main = fit_main_volume(footprint, pts, 100.0)
    assert main is not None and main.fit.roof.shape.value is RoofShape.GABLED
    # Ruído puro: nenhuma zona é explicada por um modelo.
    rng = np.random.default_rng(0)
    xyz = np.column_stack(
        [
            rng.uniform(CX - 9, CX + 9, 2000),
            rng.uniform(CY - 6, CY + 6, 2000),
            106 + rng.uniform(0, 4, 2000),
        ]
    )
    noise = PointSet(xyz, np.full(2000, 6, dtype=np.uint8))
    assert fit_main_volume(box(CX - 9, CY - 6, CX + 9, CY + 6), noise, 100.0) is None
