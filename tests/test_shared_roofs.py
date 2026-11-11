from __future__ import annotations

from pathlib import Path

from shapely.geometry import box

from osm3d.config import load_config
from osm3d.domain.models import Building, BuildingPart, RoofShape
from osm3d.reconstruction.building import apply_shared_roofs, attached_clusters, fit_params
from osm3d.reconstruction.roof_fitting import fit_roof
from tests.synthetic import CX, CY, make_roof

CFG = load_config(Path(__file__).resolve().parents[1] / "config" / "config.yaml")


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
    for b in results:
        roof = b.parts[0].roof
        assert roof.shape.value is RoofShape.GABLED
        assert roof.ridge_azimuth_deg is not None
        assert abs(roof.ridge_azimuth_deg - 90) < 2
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
