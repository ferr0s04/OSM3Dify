from __future__ import annotations

import pytest
from shapely.geometry import box

from osm3d.domain.models import BuildingPart, Estimate, Roof, RoofShape, Source


def make_part(
    shape: RoofShape = RoofShape.GABLED,
    *,
    height: float = 8.3,
    roof_height: float | None = 2.2,
    ridge_azimuth_deg: float | None = 90.0,
    facing_azimuth_deg: float | None = None,
    shape_confidence: float = 0.9,
    min_height: float = 0.0,
) -> BuildingPart:
    # Retângulo 20 m (este-oeste) × 10 m: lado maior com azimute 90°.
    return BuildingPart(
        id="part_1",
        geometry=box(0, 0, 20, 10),
        height=Estimate[float](value=height, confidence=0.95, source=Source.LIDAR),
        min_height=min_height,
        roof=Roof(
            shape=Estimate[RoofShape](
                value=shape, confidence=shape_confidence, source=Source.LIDAR
            ),
            height=(
                Estimate[float](value=roof_height, confidence=0.9, source=Source.LIDAR)
                if roof_height is not None
                else None
            ),
            ridge_azimuth_deg=ridge_azimuth_deg,
            facing_azimuth_deg=facing_azimuth_deg,
        ),
    )


@pytest.fixture
def gabled_part() -> BuildingPart:
    return make_part()
