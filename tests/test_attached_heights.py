"""Geminados/em banda: alturas iguais para o mesmo telhado, sem igualar o que é diferente."""

from __future__ import annotations

from pathlib import Path

from shapely.geometry import box

from osm3d.config import load_config
from osm3d.domain.models import Building, BuildingPart, RoofShape
from osm3d.reconstruction.building import ATTACHED_FLAG, fit_params, harmonize_attached
from osm3d.reconstruction.roof_fitting import fit_roof
from tests.synthetic import CX, CY, make_roof

CFG = load_config(Path(__file__).resolve().parents[1] / "config" / "config.yaml", local=False)


def _house(
    i: int,
    x0: float,
    shape: RoofShape,
    *,
    ground: float,
    eave: float,
    roof_height: float,
    depth: float = 10.0,
    step: float = 1.2,
    seed: int = 0,
):  # type: ignore[no-untyped-def]
    """Casa 8 m de frente × ``depth`` com a cumeeira/água ao longo da rua (eixo x)."""
    syn = make_roof(
        shape,
        length=8,
        width=depth,
        azimuth_deg=90,
        ground_z=ground,
        eave_z=eave,
        roof_height=roof_height,
        seed=seed,
    )
    pts = syn.points
    pts.xyz[:, 0] += x0  # make_roof centra em CX: desloca a casa ao longo da rua
    fp = box(CX + x0 - 4, CY - depth / 2, CX + x0 + 4, CY + depth / 2)
    fit = fit_roof(fp, pts, ground, fit_params(CFG))
    part = BuildingPart(id=f"new/{i}#1", geometry=fp, height=fit.height, roof=fit.roof)
    best = fit.best
    assert best is not None
    b = Building(
        osm_type="new",
        osm_id=i,
        osm_version=0,
        footprint=fp,
        ground_elevation_m=ground,
        parts=[part],
        diagnostics={
            "n_points": float(len(pts)),
            "inlier_ratio": best.inlier_ratio,
            "terrain_step_m": step,
        },
    )
    return b, pts


def _row(*houses):  # type: ignore[no-untyped-def]
    built = [_house(i, 8.0 * i, **h) for i, h in enumerate(houses)]
    return [b for b, _ in built], [p for _, p in built]


def test_terraced_houses_on_a_slope_get_the_same_heights() -> None:
    # Banda numa encosta: cada casa 0,25 m mais acima (chão e telhado), a mesma casa.
    # O ajuste de cada uma dá alturas ligeiramente diferentes (ruído do chão/LiDAR).
    results, pts = _row(
        dict(shape=RoofShape.GABLED, ground=100.0, eave=106.0, roof_height=3.0, seed=1),
        dict(shape=RoofShape.GABLED, ground=100.25, eave=106.25 + 0.12, roof_height=3.0, seed=2),
        dict(shape=RoofShape.GABLED, ground=100.5, eave=106.5 - 0.08, roof_height=3.0, seed=3),
    )
    before = [b.parts[0].height.value for b in results]
    assert max(before) - min(before) > 0.15

    assert harmonize_attached(results, pts, CFG) >= 2

    heights = {round(b.parts[0].height.value, 6) for b in results}
    roofs = {round(b.parts[0].roof.height.value, 6) for b in results}  # type: ignore[union-attr]
    assert len(heights) == 1 and len(roofs) == 1
    for b in results:
        part = b.parts[0]
        model = part.roof.model
        assert model is not None
        # O modelo 3D fica coerente com as tags (a partir do chão de cada casa).
        top = b.ground_elevation_m + part.height.value  # type: ignore[operator]
        assert abs(model.eave_z + model.roof_height - top) < 1e-6
    assert any(f.code == ATTACHED_FLAG for b in results for f in b.qa_flags)


def test_clearly_different_heights_stay() -> None:
    # Terreno plano: 0,6 m de diferença no telhado é real.
    results, pts = _row(
        dict(shape=RoofShape.GABLED, ground=100.0, eave=106.0, roof_height=3.0, step=0.1),
        dict(shape=RoofShape.GABLED, ground=100.0, eave=106.6, roof_height=3.0, step=0.1, seed=4),
    )
    assert harmonize_attached(results, pts, CFG) == 0


def test_flat_terrain_small_real_step_stays() -> None:
    # Planos encostados em terreno plano, 0,25 m de diferença: o chão é o mesmo, a
    # diferença vê-se no LiDAR e não é ruído.
    results, pts = _row(
        dict(shape=RoofShape.FLAT, ground=100.0, eave=104.0, roof_height=0.0, step=0.1),
        dict(shape=RoofShape.FLAT, ground=100.0, eave=104.25, roof_height=0.0, step=0.1, seed=5),
    )
    assert harmonize_attached(results, pts, CFG) == 0


def test_different_roof_form_stays() -> None:
    # Mesma altura total, mas um é mais fundo (cumeeira noutra posição): telhados
    # diferentes, o modelo de um não explica os pontos do outro.
    results, pts = _row(
        dict(shape=RoofShape.GABLED, ground=100.0, eave=106.0, roof_height=3.0, depth=10.0),
        dict(shape=RoofShape.GABLED, ground=100.0, eave=105.7, roof_height=3.3, depth=12.0, seed=6),
    )
    assert harmonize_attached(results, pts, CFG) == 0


def test_disabled_by_config() -> None:
    results, pts = _row(
        dict(shape=RoofShape.GABLED, ground=100.0, eave=106.0, roof_height=3.0, seed=1),
        dict(shape=RoofShape.GABLED, ground=100.25, eave=106.37, roof_height=3.0, seed=2),
    )
    heights = CFG.heights.model_copy(update={"attached_harmonize": False})
    cfg = CFG.model_copy(update={"heights": heights})
    assert harmonize_attached(results, pts, cfg) == 0
