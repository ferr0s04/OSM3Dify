"""Mapeamento do modelo intermédio para tags Simple 3D Buildings (S3DB).

É o único sítio onde se decide que tags OSM são produzidas.
Referência: https://wiki.openstreetmap.org/wiki/Simple_3D_Buildings
"""

from __future__ import annotations

from dataclasses import dataclass, field

from osm3d.domain.models import BuildingPart, RoofShape
from osm3d.geometry import axis_difference_deg, longest_side_azimuth_deg

# Formas com cumeeira cuja direção se exprime via roof:orientation (along/across).
_RIDGED_SHAPES = {RoofShape.GABLED, RoofShape.HIPPED}


@dataclass(frozen=True)
class TagPolicy:
    min_shape_confidence: float = 0.7
    min_colour_confidence: float = 0.6
    height_rounding_m: float = 0.5
    roof_height_rounding_m: float = 0.5


@dataclass
class TagMerge:
    """Resultado de juntar as tags propostas às tags existentes no OSM."""

    tags: dict[str, str]
    added: dict[str, str] = field(default_factory=dict)
    changed: dict[str, tuple[str, str]] = field(default_factory=dict)
    conflicts: dict[str, tuple[str, str]] = field(default_factory=dict)

    @property
    def has_changes(self) -> bool:
        return bool(self.added or self.changed)


def format_metres(value: float, step: float) -> str:
    """Arredonda a ``step`` e formata sem zeros desnecessários (``8.0`` → ``"8"``)."""
    rounded = round(value / step) * step if step > 0 else value
    text = f"{rounded:.2f}".rstrip("0").rstrip(".")
    return "0" if text in ("", "-0") else text


def part_to_tags(part: BuildingPart, policy: TagPolicy | None = None) -> dict[str, str]:
    """Tags S3DB para uma parte (ou para o contorno, num edifício de uma só parte)."""
    policy = policy or TagPolicy()
    tags: dict[str, str] = {
        "height": format_metres(part.height.value, policy.height_rounding_m),
    }
    if part.min_height > 0:
        tags["min_height"] = format_metres(part.min_height, policy.height_rounding_m)

    roof = part.roof
    shape = roof.shape.value
    if shape is RoofShape.UNKNOWN or roof.shape.confidence < policy.min_shape_confidence:
        shape = RoofShape.UNKNOWN
    else:
        tags["roof:shape"] = shape.value

    if shape not in (RoofShape.UNKNOWN, RoofShape.FLAT) and roof.height is not None:
        tags["roof:height"] = format_metres(roof.height.value, policy.roof_height_rounding_m)

    if shape in _RIDGED_SHAPES and roof.ridge_azimuth_deg is not None:
        long_axis = longest_side_azimuth_deg(part.geometry)
        # "along" é o valor por omissão nos renderizadores: só se marca "across".
        if axis_difference_deg(roof.ridge_azimuth_deg, long_axis) > 45.0:
            tags["roof:orientation"] = "across"

    if shape is RoofShape.SKILLION and roof.facing_azimuth_deg is not None:
        tags["roof:direction"] = str(round(roof.facing_azimuth_deg) % 360)

    if roof.colour is not None and roof.colour.confidence >= policy.min_colour_confidence:
        tags["roof:colour"] = roof.colour.value
    if (
        part.facade_colour is not None
        and part.facade_colour.confidence >= policy.min_colour_confidence
    ):
        tags["building:colour"] = part.facade_colour.value

    return tags


# Valores OSM sinónimos: o valor existente mantém-se e não é conflito.
_SYNONYMS: dict[str, dict[str, str]] = {"roof:shape": {"lean_to": "skillion"}}


def equivalent(key: str, a: str, b: str) -> bool:
    syn = _SYNONYMS.get(key, {})

    def norm(v: str) -> str:
        v = v.strip().lower()
        return syn.get(v, v)

    return norm(a) == norm(b)


def merge_tags(
    existing: dict[str, str], proposed: dict[str, str], *, overwrite: bool = False
) -> TagMerge:
    """Junta tags propostas às existentes sem destruir trabalho de mapeadores.

    Por omissão, uma tag existente com valor diferente é registada como conflito
    e mantida; com ``overwrite=True`` é substituída e registada em ``changed``.
    """
    result = TagMerge(tags=dict(existing))
    for key, new in proposed.items():
        old = existing.get(key)
        if old is None:
            result.tags[key] = new
            result.added[key] = new
        elif not equivalent(key, old, new):
            if overwrite:
                result.tags[key] = new
                result.changed[key] = (old, new)
            else:
                result.conflicts[key] = (old, new)
    return result
