from __future__ import annotations

import pytest

from osm3d.domain.models import Estimate, RoofShape, Source
from osm3d.export.s3db_tags import TagPolicy, format_metres, merge_tags, part_to_tags
from osm3d.geometry import axis_difference_deg, longest_side_azimuth_deg
from tests.conftest import make_part


@pytest.mark.parametrize(
    ("value", "step", "expected"),
    [(8.3, 0.5, "8.5"), (8.0, 0.5, "8"), (7.74, 0.5, "7.5"), (12.34, 0.1, "12.3"), (0.1, 0.5, "0")],
)
def test_format_metres(value: float, step: float, expected: str) -> None:
    assert format_metres(value, step) == expected


def test_axis_difference_is_symmetric_and_mod_180() -> None:
    assert axis_difference_deg(10, 170) == pytest.approx(20)
    assert axis_difference_deg(0, 90) == pytest.approx(90)
    assert axis_difference_deg(45, 225) == pytest.approx(0)


def test_longest_side_azimuth(gabled_part) -> None:  # type: ignore[no-untyped-def]
    assert axis_difference_deg(longest_side_azimuth_deg(gabled_part.geometry), 90) < 1e-6


def test_gabled_along_long_side_has_no_orientation_tag(gabled_part) -> None:  # type: ignore[no-untyped-def]
    tags = part_to_tags(gabled_part)
    assert tags == {"height": "8.5", "roof:shape": "gabled", "roof:height": "2"}


def test_gabled_across_long_side() -> None:
    tags = part_to_tags(make_part(ridge_azimuth_deg=0.0))
    assert tags["roof:orientation"] == "across"


def test_low_confidence_shape_is_omitted() -> None:
    tags = part_to_tags(make_part(shape_confidence=0.4), TagPolicy(min_shape_confidence=0.7))
    assert "roof:shape" not in tags
    assert "roof:height" not in tags
    assert "height" in tags


def test_flat_roof_has_no_roof_height() -> None:
    tags = part_to_tags(make_part(RoofShape.FLAT, roof_height=0.0, ridge_azimuth_deg=None))
    assert tags["roof:shape"] == "flat"
    assert "roof:height" not in tags


def test_skillion_direction() -> None:
    part = make_part(RoofShape.SKILLION, ridge_azimuth_deg=None, facing_azimuth_deg=179.6)
    assert part_to_tags(part)["roof:direction"] == "180"


def test_min_height_only_when_positive() -> None:
    assert "min_height" not in part_to_tags(make_part())
    assert part_to_tags(make_part(min_height=3.0))["min_height"] == "3"


def test_colour_respects_confidence() -> None:
    part = make_part()
    part.roof.colour = Estimate[str](value="brown", confidence=0.8, source=Source.ORTHOPHOTO)
    assert part_to_tags(part)["roof:colour"] == "brown"
    part.roof.colour = Estimate[str](value="brown", confidence=0.3, source=Source.ORTHOPHOTO)
    assert "roof:colour" not in part_to_tags(part)


def test_merge_never_overwrites_by_default() -> None:
    existing = {"building": "yes", "height": "9"}
    result = merge_tags(existing, {"height": "8.5", "roof:shape": "gabled"})
    assert result.tags == {"building": "yes", "height": "9", "roof:shape": "gabled"}
    assert result.added == {"roof:shape": "gabled"}
    assert result.conflicts == {"height": ("9", "8.5")}
    assert result.has_changes


def test_merge_with_overwrite() -> None:
    result = merge_tags({"height": "9"}, {"height": "8.5"}, overwrite=True)
    assert result.tags["height"] == "8.5"
    assert result.changed == {"height": ("9", "8.5")}


def test_merge_synonyms_are_not_conflicts() -> None:
    result = merge_tags({"roof:shape": "lean_to"}, {"roof:shape": "skillion"})
    assert result.tags["roof:shape"] == "lean_to"
    assert not result.conflicts and not result.has_changes


def test_merge_identical_is_noop() -> None:
    result = merge_tags({"height": "8.5"}, {"height": "8.5"})
    assert not result.has_changes
    assert not result.conflicts
