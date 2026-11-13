from __future__ import annotations

import numpy as np
import pytest
from shapely.geometry import box

from osm3d.appearance.area import colour_parts
from osm3d.appearance.roof_colours import (
    PALETTE,
    SensorColour,
    classify_lab,
    classify_ortho_lab,
    dominant_roof_colour,
    estimate_roof_colour,
    fuse,
    roof_facets,
    roof_points_mask,
    sensor_colour,
    to_lab,
)
from osm3d.domain.models import Building, RoofModel, RoofShape, Source
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
RED_SHEET = (200, 150, 160)  # chapa vermelha desbotada (tom < 12°, C* > 10)


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
    assert est is not None and est.confidence < 0.6


def test_red_only_crimson_or_on_low_roofs() -> None:
    lab = to_lab(np.array([RED_SHEET]))
    assert classify_lab(lab)[0] == "red"
    # Tom de telha avermelhada (≈ 15°): vermelho só em telhado plano / de uma água.
    reddish_tile = np.array([[68.0, 18.0, 5.0]])  # tom ≈ 15,5°, C* ≈ 18,7
    assert classify_lab(reddish_tile, low_pitch=False)[0] == "tile_dark"
    assert classify_lab(reddish_tile, low_pitch=True)[0] == "red"


@pytest.mark.parametrize(
    ("lab", "low", "key"),
    [
        ((63, 11, 10), False, "tile"),  # ORTOS: telha (tom ≈ 42°, C* ≈ 15)
        ((55, 6, 3), False, "tile_dark"),
        ((60, 11, 3), False, "tile"),  # tom ≈ 15°: telha em telhado inclinado
        ((60, 11, 3), True, "red"),  # … chapa/tela vermelha em telhado plano
        ((52, 9.7, -0.2), False, "red"),  # carmim (tom ≈ −1°): vermelho em qualquer telhado
        ((65, 8, 17), False, "orange"),  # tom ≈ 65°, C* ≈ 19
        ((50, -7, -7), False, "green"),
        ((47, -1, -6), False, "dark_grey"),  # cinzentos do ortofoto: ligeiramente azulados
        ((63, -1, -3), False, "grey"),
        ((72, -1, -3), False, "light_grey"),
        ((92, -1, -2), False, "white"),
    ],
)
def test_classifies_ortho_colours(lab: tuple[float, float, float], low: bool, key: str) -> None:
    assert classify_ortho_lab(np.array([lab], float), low_pitch=low)[0] == key


def test_facet_lighting_does_not_lower_confidence() -> None:
    # Duas águas da mesma telha, uma ao sol (L* 80) e outra à sombra (L* 64).
    rng = np.random.default_rng(0)
    sun = np.column_stack([rng.normal(80, 3, 200), rng.normal(14, 2, 200), rng.normal(13, 2, 200)])
    shade = sun - np.array([16.0, 0, 0])
    lab = np.vstack([sun, shade])
    facets = np.repeat([1, 3], 200)
    xy = np.column_stack([rng.uniform(0, 10, 400), np.repeat([2.0, 8.0], 200)])
    est = sensor_colour(lab, classify_lab, facets, xy)
    assert est is not None and est.key == "tile" and est.agreement > 0.9
    # Sem as águas, metade das células passa a telha escura (meia concordância).
    flat = sensor_colour(lab, classify_lab, None, xy)
    assert flat is not None and flat.agreement < est.agreement


def test_fusion_prefers_ortho_and_weighs_agreement() -> None:
    ortho = SensorColour("tile", (63.0, 11.0, 10.0), 0.7, 300)
    agree = fuse(SensorColour("tile_dark", (68.0, 9.0, 8.0), 0.5, 300), ortho)
    disagree = fuse(SensorColour("light_grey", (75.0, 0.0, 1.0), 0.9, 300), ortho)
    assert agree is not None and disagree is not None
    assert agree.value == disagree.value == PALETTE["tile"].hex
    assert agree.source == Source.ORTHOPHOTO
    assert agree.confidence > 0.7 > disagree.confidence
    lidar_only = fuse(SensorColour("grey", (60.0, 0.0, 0.0), 0.8, 50), None)
    assert lidar_only is not None and lidar_only.source == Source.LIDAR
    assert lidar_only.confidence == pytest.approx(0.4)


def test_mask_excludes_edges_and_off_surface_points() -> None:
    fp = box(0, 0, 10, 10)
    b = Building(osm_type="way", osm_id=1, osm_version=1, footprint=fp)
    xyz = np.array([[5, 5, 10.0], [0.2, 5, 10.0], [5, 5, 10.0]])
    cls = np.array([6, 6, 5], dtype=np.uint8)
    mask = roof_points_mask(b, PointSet(xyz, cls))
    assert mask.tolist() == [True, False, False]  # bordo e vegetação excluídos


def _gabled_building() -> Building:
    fp = box(0, 0, 20, 10)
    model = RoofModel(
        cx=10, cy=5, azimuth_deg=90, half_length=10, half_width=5, eave_z=6, roof_height=3
    )
    part = make_part(RoofShape.GABLED).model_copy(update={"geometry": fp})
    part = part.model_copy(update={"roof": part.roof.model_copy(update={"model": model})})
    return Building(osm_type="way", osm_id=1, osm_version=1, footprint=fp, parts=[part])


def test_roof_facets_split_gabled_slopes() -> None:
    b = _gabled_building()
    x = np.array([10.0, 10.0, 5.0, 15.0])
    y = np.array([2.0, 8.0, 2.5, 7.5])
    k = roof_facets(b, x, y)
    assert k[0] == k[2] != 0 and k[1] == k[3] != 0 and k[0] != k[1]


class _FlatOrtho:
    """Ortofoto de teste: uma só cor em toda a parte (R, G, B, NIR)."""

    def __init__(self, rgb: tuple[int, int, int], nir: int = 60) -> None:
        self.px = (*rgb, nir)

    def covers(self, bounds: tuple[float, float, float, float]) -> bool:
        return True

    def read(self, bounds: tuple[float, float, float, float]) -> np.ndarray:
        w = round((bounds[2] - bounds[0]) / 0.25)
        h = round((bounds[3] - bounds[1]) / 0.25)
        return np.broadcast_to(np.array(self.px, np.float32)[:, None, None], (4, h, w)).copy()


def _gabled_points(rgb: tuple[int, int, int], n: int = 400) -> PointSet:
    b = _gabled_building()
    rng = np.random.default_rng(1)
    x, y = rng.uniform(1, 19, n), rng.uniform(1, 9, n)
    z = 9 - 3 * np.abs(y - 5) / 5  # cumeeira a meio (y = 5), beiral a 6 m
    assert b.parts[0].roof.model is not None
    rgb16 = (_pts(rgb, n) * 256).astype(np.uint16)
    return PointSet(np.column_stack([x, y, z]), np.full(n, 6, np.uint8), rgb16)


def test_ortho_colour_confirmed_by_lidar() -> None:
    b = _gabled_building()
    est = estimate_roof_colour(b, _gabled_points(TILE_WASHED), _FlatOrtho((170, 140, 128)))  # type: ignore[arg-type]
    assert est is not None and est.source == Source.ORTHOPHOTO
    assert est.value == PALETTE["tile"].hex and est.confidence > 0.9
    # Vegetação no ortofoto (NDVI alto): fica a cor do LiDAR.
    veg = estimate_roof_colour(b, _gabled_points(TILE_WASHED), _FlatOrtho((60, 90, 50), nir=200))  # type: ignore[arg-type]
    assert veg is not None and veg.source == Source.LIDAR


def test_colour_parts_colours_every_part() -> None:
    b = _gabled_building()
    left = b.parts[0].model_copy(update={"id": "a", "geometry": box(0, 0, 10, 10)})
    right = b.parts[0].model_copy(update={"id": "b", "geometry": box(10, 0, 20, 10)})
    two = b.model_copy(update={"parts": [left, right]})
    out = colour_parts(two, _gabled_points(TILE_WASHED, 800), _FlatOrtho((170, 140, 128)))
    assert all(p.roof.colour is not None for p in out.parts)
    assert {p.roof.colour.value for p in out.parts if p.roof.colour} == {PALETTE["tile"].hex}


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
