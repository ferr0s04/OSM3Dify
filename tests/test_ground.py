from __future__ import annotations

import numpy as np
import pytest
from shapely.geometry import box

from osm3d.domain.models import Building, GroundInfo
from osm3d.reconstruction.ground import choose_ground, parse_levels
from osm3d.review import Overrides, apply_corrections
from tests.conftest import make_part

# Perfil do pé das paredes de way/1441884233: lote a ~50,2 m em três lados, estrada
# ~4,6 m abaixo num deles.
TERRACE = np.array([50.2] * 30 + [45.6] * 10)
FLAT = np.array([50.0, 50.1, 50.2, 50.1] * 10)


def test_flat_terrain_keeps_low_ground() -> None:
    g = choose_ground(49.9, FLAT, eave_z=53.0, levels=1)
    assert g.choice == "low" and g.basis == "flat" and g.step_m < 1


def test_terrace_resolved_by_levels() -> None:
    # Beiral a 53,2: 3,0 m acima do lote (1 piso ✓) vs. 7,8 m acima da estrada.
    g = choose_ground(45.4, TERRACE, eave_z=53.2, levels=1)
    assert g.step_m > 4 and g.choice == "high" and g.basis == "levels"
    assert g.elevation() == pytest.approx(50.2)


def test_two_storeys_on_slope_keep_low_ground() -> None:
    # 2 pisos: 7,8 m acima da estrada (3,9 m/piso) é mais plausível do que 3,0 m (1,5).
    g = choose_ground(45.4, TERRACE, eave_z=53.2, levels=2)
    assert g.choice == "low" and g.basis == "levels"


def test_built_into_slope_uses_low_ground() -> None:
    # O lado de cima (lote) está só 1 m abaixo do beiral: edifício encaixado na encosta.
    g = choose_ground(45.4, TERRACE, eave_z=51.2, levels=None)
    assert g.choice == "low" and g.basis == "eave"


def test_no_evidence_defaults_to_low() -> None:
    g = choose_ground(45.4, TERRACE, eave_z=53.2, levels=None)
    assert g.choice == "low" and g.basis == "none"


@pytest.mark.parametrize(
    ("raw", "expected"), [("2", 2.0), ("1.5", 1.5), ("x", None), (None, None), ("0", None)]
)
def test_parse_levels(raw: str | None, expected: float | None) -> None:
    assert parse_levels(raw) == expected


def test_manual_ground_choice_keeps_roof_top() -> None:
    part = make_part(height=9.0)  # medido a partir da estrada
    b = Building(
        osm_type="way",
        osm_id=1,
        osm_version=1,
        footprint=box(0, 0, 20, 10),
        ground_elevation_m=45.5,
        parts=[part],
        ground=GroundInfo(low_m=45.5, high_m=50.0, step_m=4.5, choice="low", basis="none"),
    )
    lot = apply_corrections(b, Overrides(ground="high"))
    assert lot.ground_elevation_m == 50.0
    assert lot.parts[0].height.value == pytest.approx(4.5)  # topo continua a 54,5 m
    assert lot.ground is not None and lot.ground.basis == "manual"
    # Voltar ao chão baixo repõe a altura.
    back = apply_corrections(lot, Overrides(ground="low"))
    assert back.parts[0].height.value == pytest.approx(9.0)
