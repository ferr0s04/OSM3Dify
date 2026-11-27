from __future__ import annotations

from pathlib import Path

import numpy as np
from shapely.geometry import box

from osm3d.config import load_config
from osm3d.domain.models import Building, BuildingPart, RoofShape
from osm3d.reconstruction.building import apply_shared_roofs, attached_clusters, fit_params
from osm3d.reconstruction.roof_fitting import fit_roof
from tests.synthetic import CX, CY, make_roof

CFG = load_config(Path(__file__).resolve().parents[1] / "config" / "config.yaml", local=False)


def _halves(shape: RoofShape) -> tuple[list[Building], list, list]:  # type: ignore[type-arg]
    # Telhado 20 x 10 m com cumeeira este-oeste, dividido ao longo da cumeeira
    # (casa geminada): cada metade sozinha é uma só água.
    syn = make_roof(shape, length=20, width=10, azimuth_deg=90, roof_height=2.5, seed=11)
    north = syn.points.xyz[:, 1] >= CY
    halves = [
        (box(CX - 10, CY, CX + 10, CY + 5), north),
        (box(CX - 10, CY - 5, CX + 10, CY), ~north),
    ]
    results, fits, point_sets = [], [], []
    for i, (fp, mask) in enumerate(halves):
        pts = syn.points.subset(mask)
        fit = fit_roof(fp, pts, syn.ground_z, fit_params(CFG))
        part = BuildingPart(id=f"way/{i}#1", geometry=fp, height=fit.height, roof=fit.roof)
        results.append(
            Building(
                osm_type="way",
                osm_id=i,
                osm_version=1,
                footprint=fp,
                ground_elevation_m=syn.ground_z,
                parts=[part],
            )
        )
        fits.append(fit)
        point_sets.append(pts)
    return results, fits, point_sets


def test_attached_clusters() -> None:
    fps = [box(0, 0, 10, 5), box(0, 5.1, 10, 10), box(30, 0, 40, 10)]
    assert attached_clusters(fps) == [[0, 1]]


def test_semi_detached_gabled_becomes_shared_roof() -> None:
    results, fits, point_sets = _halves(RoofShape.GABLED)
    assert {b.parts[0].roof.shape.value for b in results} == {RoofShape.SKILLION}

    changed = apply_shared_roofs(results, fits, point_sets, (0.0, 0.0), CFG)

    assert changed == 2
    # As tags de cada metade descrevem o telhado sobre a planta dela: uma água virada para
    # fora, até à cumeeira comum (um gabled em cada metade desenhar-se-ia com duas águas).
    for b, facing in zip(results, (0.0, 180.0), strict=True):
        roof = b.parts[0].roof
        assert roof.shape.value is RoofShape.SKILLION
        assert roof.facing_azimuth_deg is not None
        assert abs((roof.facing_azimuth_deg - facing + 180) % 360 - 180) < 2
        assert roof.height is not None and abs(roof.height.value - 2.5) < 0.1
        assert any(f.code == "shared_roof" for f in b.qa_flags)
        assert abs(b.parts[0].height.value - 8.5) < 0.2  # 6 m + 2,5 m


def test_independent_roofs_stay_separate() -> None:
    # Duas metades com telhado de uma água cada, viradas para lados opostos e com
    # cotas diferentes: um telhado único explica-as pior.
    results, fits, point_sets = _halves(RoofShape.GABLED)
    north = point_sets[0]
    north.xyz[:, 2] += 2.0  # metade norte 2 m mais alta: não é um telhado contínuo
    fits[0] = fit_roof(results[0].footprint, north, 100.0, fit_params(CFG))
    assert apply_shared_roofs(results, fits, point_sets, (0.0, 0.0), CFG) == 0


def test_terraced_hipped_roof_end_houses_are_side_hipped() -> None:
    """Hipped contínuo sobre 3 casas em banda: as das pontas ficam side_hipped (água na
    ponta de fora, empena na parede comum) e a do meio gabled. O modelo 3D de cada uma é
    o que as tags dela desenham."""
    from osm3d.reconstruction.building import member_roof
    from osm3d.reconstruction.roof_models import model_surface_z

    syn = make_roof(
        RoofShape.HIPPED, length=24, width=10, azimuth_deg=90, roof_height=3.0, ridge_ratio=0.5
    )
    fit = fit_roof(syn.footprint, syn.points, syn.ground_z, fit_params(CFG))
    assert fit.roof.shape.value is RoofShape.HIPPED
    houses = [box(CX + x0, CY - 5, CX + x0 + 8, CY + 5) for x0 in (-12, -4, 4)]
    shapes = []
    for fp in houses:
        own = member_roof(fit.roof, syn.footprint, fp, syn.ground_z, fit_params(CFG))
        assert own is not None and own.roof.model is not None
        shapes.append(own.roof.shape.value)
        # O modelo próprio reproduz o telhado do grupo sobre a casa.
        x, y = np.array([fp.centroid.x]), np.array([fp.centroid.y])
        z_own = model_surface_z(own.roof.model, own.roof.shape.value, x, y, 0.0)
        z_group = model_surface_z(fit.roof.model, RoofShape.HIPPED, x, y, 0.0)
        assert abs(z_own[0] - z_group[0]) < 0.15
    assert shapes == [RoofShape.SIDE_HIPPED, RoofShape.GABLED, RoofShape.SIDE_HIPPED]
