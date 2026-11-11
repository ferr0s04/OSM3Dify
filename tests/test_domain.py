from __future__ import annotations

import pytest
from pydantic import ValidationError
from shapely.geometry import Point, box

from osm3d.domain.models import Building, BuildingPart, Estimate, Source
from tests.conftest import make_part


def test_part_roundtrips_through_json(gabled_part: BuildingPart) -> None:
    data = gabled_part.model_dump_json()
    restored = BuildingPart.model_validate_json(data)
    assert restored.geometry.equals(gabled_part.geometry)
    assert restored.roof.shape == gabled_part.roof.shape


def test_confidence_must_be_in_unit_interval() -> None:
    with pytest.raises(ValidationError):
        Estimate[float](value=1.0, confidence=1.5, source=Source.LIDAR)


def test_geometry_must_be_polygonal() -> None:
    with pytest.raises(ValidationError):
        Building(osm_type="way", osm_id=1, osm_version=1, footprint=Point(0, 0))


def test_building_single_part() -> None:
    b = Building(
        osm_type="way", osm_id=1, osm_version=3, footprint=box(0, 0, 20, 10), parts=[make_part()]
    )
    assert b.is_single_part
    assert not b.needs_review
