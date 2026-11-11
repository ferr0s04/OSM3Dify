from __future__ import annotations

import numpy as np
import pytest
from shapely.geometry import box

from osm3d.appearance.roof_colours import (
    PALETTE,
    dominant_roof_colour,
    estimate_roof_colour,
    roof_points_mask,
)
from osm3d.domain.models import Building, RoofShape
from osm3d.export.s3db_tags import part_to_tags
from osm3d.geospatial.point_clouds import PointSet
from osm3d.review import Overrides, apply_overrides
from tests.conftest import make_part

# Cores RGB medianas reais do LiDAR DGT em Vila do Conde (desbotadas).
TILE_WASHED = (245, 207, 187)  # telha, hipped
TILE_DARK = (184, 160, 155)  # telha envelhecida
CONCRETE = (196, 195, 191)  # laje plana
WHITE = (231, 229, 226)
DARK = (90, 92, 95)


def _pts(rgb: tuple[int, int, int], n: int = 300, noise: float = 6, seed: int = 0) -> np.ndarray:
    rng = np.random.default_rng(seed)
    return np.clip(np.array(rgb) + rng.normal(0, noise, (n, 3)), 0, 255).astype(int)


@pytest.mark.parametrize(
    ("rgb", "key"),
    [
        (TILE_WASHED, "tile"),
        (TILE_DARK, "tile_dark"),
        (CONCRETE, "light_grey"),
        (WHITE, "white"),
        (DARK, "dark_grey"),
    ],
)
def test_classifies_washed_out_lidar_colours(rgb: tuple[int, int, int], key: str) -> None:
    est = dominant_roof_colour(_pts(rgb))
    assert est is not None
    assert est.value == PALETTE[key].hex
    assert est.confidence > 0.8


def test_few_points_or_mixed_lower_confidence() -> None:
    assert dominant_roof_colour(_pts(TILE_WASHED, n=5)) is None
    mixed = np.vstack([_pts(TILE_WASHED, 160), _pts(CONCRETE, 140, seed=1)])
    est = dominant_roof_colour(mixed)
    assert est is not None and est.value == PALETTE["tile"].hex and est.confidence < 0.6


def test_mask_excludes_edges_and_off_surface_points() -> None:
    fp = box(0, 0, 10, 10)
    b = Building(osm_type="way", osm_id=1, osm_version=1, footprint=fp)
    xyz = np.array([[5, 5, 10.0], [0.2, 5, 10.0], [5, 5, 10.0]])
    cls = np.array([6, 6, 5], dtype=np.uint8)
    mask = roof_points_mask(b, PointSet(xyz, cls))
    assert mask.tolist() == [True, False, False]  # bordo e vegetação excluídos


def test_16bit_rgb_and_tag_output() -> None:
    fp = box(0, 0, 10, 10)
    part = make_part(RoofShape.FLAT).model_copy(update={"geometry": fp})
    b = Building(osm_type="way", osm_id=1, osm_version=1, footprint=fp, parts=[part])
    n = 300
    rng = np.random.default_rng(0)
    xyz = np.column_stack([rng.uniform(1, 9, n), rng.uniform(1, 9, n), np.full(n, 8.0)])
    rgb16 = (_pts(TILE_WASHED, n) * 256).astype(np.uint16)
    est = estimate_roof_colour(b, PointSet(xyz, np.full(n, 6, np.uint8), rgb16))
    assert est is not None and est.value == PALETTE["tile"].hex
    tagged = part.model_copy(update={"roof": part.roof.model_copy(update={"colour": est})})
    assert part_to_tags(tagged)["roof:colour"] == "#b5533c"


def test_colour_overrides() -> None:
    fp = box(0, 0, 10, 10)
    part = make_part(RoofShape.GABLED).model_copy(update={"geometry": fp})
    b = Building(
        osm_type="way", osm_id=1, osm_version=1, footprint=fp, parts=[part], ground_elevation_m=0.0
    )
    new = apply_overrides(b, part, Overrides(roof_colour="#AA0000", building_colour="#ffffff"))
    tags = part_to_tags(new)
    assert tags["roof:colour"] == "#aa0000" and tags["building:colour"] == "#ffffff"
    with pytest.raises(ValueError):
        Overrides(roof_colour="red")
