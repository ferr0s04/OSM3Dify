"""Contornos do ortofoto guiados pelo LiDAR (cenas sintéticas)."""

from __future__ import annotations

from pathlib import Path
from xml.etree import ElementTree as ET

import numpy as np
import shapely
from rasterio.features import rasterize
from rasterio.transform import from_origin
from shapely.geometry import Polygon, box

from osm3d.domain.models import Building
from osm3d.export.osm_xml import write_josm_file
from osm3d.reconstruction.ortho_outline import (
    RES_M,
    Patch,
    _grad,
    buildings_from_seed,
    edge_scale,
    remove_chamfers,
    snap_edges,
)
from tests.test_footprints import OSM_XML

BOUNDS = (0.0, 0.0, 30.0, 20.0)


def _patch(roofs: list[tuple[Polygon, float, float]]) -> Patch:
    """Ortofoto e alturas sintéticas: cada telhado é (polígono, brilho, altura)."""
    x0, y0, x1, y1 = BOUNDS
    shape_ = (round((y1 - y0) / RES_M), round((x1 - x0) / RES_M))
    t = from_origin(x0, y1, RES_M, RES_M)
    gray = np.full(shape_, 60.0)
    h = np.zeros(shape_, np.float32)
    has = np.zeros(shape_, bool)
    for poly, bright, height in roofs:
        m = rasterize([(poly, 1)], out_shape=shape_, transform=t).astype(bool)
        gray[m] = bright
        h[m] = height
        has |= m
    gx, gy = _grad(gray, 1.0)
    return Patch(BOUNDS, gx, gy, h, has, np.zeros(shape_), edge_scale(gx, gy))


def test_remove_chamfers_restores_corner() -> None:
    chamfered = Polygon([(0, 0), (10, 0), (10, 4.5), (8.5, 6), (0, 6)])
    out = remove_chamfers(chamfered, 0.0)
    assert len(out.exterior.coords) - 1 == 4
    assert abs(out.area - 60.0) < 0.01


def test_remove_chamfers_keeps_unsupported_diagonal() -> None:
    # Sem pontos LiDAR no canto que seria acrescentado: a parede oblíqua fica.
    real = Polygon([(2, 2), (20, 2), (20, 10), (14, 16), (2, 16)])
    patch = _patch([(real, 200, 5.0)])
    out = remove_chamfers(real, 0.0, patch)
    assert out.equals(real)


def test_snap_edges_moves_to_roof_edge() -> None:
    roof = box(5, 4, 20, 14)
    patch = _patch([(roof, 200, 6.0)])
    start = box(5.75, 4.5, 19.0, 13.25)  # contorno de LiDAR, encolhido/deslocado
    out = snap_edges(patch, start)
    assert out.symmetric_difference(roof).area < 0.06 * roof.area
    for a, b in zip(sorted(out.bounds), sorted(roof.bounds), strict=True):
        assert abs(a - b) <= 0.25


def test_buildings_from_seed_splits_by_height() -> None:
    high, low = box(4, 4, 14, 16), box(14, 4, 26, 12)
    patch = _patch([(high, 180, 7.0), (low, 120, 3.0)])
    seed = high.union(low)
    out = buildings_from_seed(seed, patch)
    assert len(out) == 2
    a, b = sorted(out, key=lambda p: -p.area)
    assert a.intersection(b).area < 0.01
    assert a.touches(b) or a.distance(b) < 0.05  # parede partilhada
    assert abs(a.centroid.x - high.centroid.x) < 0.75
    assert abs(b.centroid.x - low.centroid.x) < 0.75


def test_buildings_from_seed_single_roof() -> None:
    roof = box(6, 5, 22, 15)
    patch = _patch([(roof, 200, 5.0)])
    out = buildings_from_seed(roof.buffer(-0.5), patch)
    assert len(out) == 1
    assert out[0].symmetric_difference(roof).area < 0.06 * roof.area


def test_contiguous_new_buildings_share_nodes(tmp_path: Path) -> None:
    src = tmp_path / "area.osm"
    src.write_text(OSM_XML, encoding="utf-8")
    origin = np.array([-45000.0, 182000.0])
    a = shapely.affinity.translate(box(0, 0, 10, 8), *origin)
    b = shapely.affinity.translate(box(10, 0, 16, 5), *origin)  # T-junção em (10, 5)
    sel = [
        (Building(osm_type="new", osm_id=i, osm_version=0, footprint=g), {"building": "yes"})
        for i, g in enumerate((a, b), start=1)
    ]
    root = ET.parse(write_josm_file(sel, src, tmp_path / "out.osm")).getroot()
    ways = [[nd.attrib["ref"] for nd in w.findall("nd")] for w in root.findall("way")]
    assert len(ways) == 2
    shared = set(ways[0]) & set(ways[1])
    assert len(shared) == 2  # (10, 0) e a junção em T (10, 5), inserida no lado de a


def test_fit_to_land_clips_road_and_small_landuse_excess() -> None:
    from osm3d.reconstruction.candidates import LandContext, fit_to_land

    built = box(0, 0, 100, 50)
    other = box(0, 50, 100, 100)
    road = box(0, -10, 100, 2)  # faixa de rodagem a invadir 2 m da base
    fp = box(10, 0, 30, 52)  # 2 m para o agrícola (4 %)
    out, flags = fit_to_land(fp, LandContext(built=built, other=other, roads=road))
    assert out is not None and not flags
    assert out.bounds == (10.0, 2.0, 30.0, 50.0)


def test_fit_to_land_keeps_large_excess_and_flags_outside() -> None:
    from osm3d.reconstruction.candidates import LandContext, fit_to_land

    built = box(0, 0, 100, 50)
    other = box(0, 50, 100, 100)
    straddling = box(10, 40, 30, 60)  # metade fora: o landuse é que está mal
    out, _ = fit_to_land(straddling, LandContext(built=built, other=other))
    assert out is not None and abs(out.area - straddling.area) < 1e-6
    barn = box(10, 70, 30, 90)
    out, flags = fit_to_land(barn, LandContext(built=built, other=other))
    assert out is not None and [f.code for f in flags] == ["outside_built_landuse"]


def test_split_rows_cuts_terraced_houses_at_colour_change() -> None:
    from rasterio.transform import rowcol

    from osm3d.reconstruction.ortho_outline import split_rows

    roof = box(2, 4, 26, 14)  # cumeeira ao longo de x, em y = 9
    patch = _patch([(roof, 200, 5.0)])
    rows, cols = np.indices(patch.h.shape)
    x = BOUNDS[0] + (cols + 0.5) * RES_M
    y = BOUNDS[3] - (rows + 0.5) * RES_M
    inside = patch.has
    patch.h[inside] = (6.0 - 0.6 * np.abs(y - 9.0))[inside]  # duas águas
    rgb = np.full((3, *patch.h.shape), 60.0)
    rgb[:, inside & (x < 14)] = np.array([[200.0], [90.0], [60.0]])  # telha laranja
    rgb[:, inside & (x >= 14)] = np.array([[120.0], [110.0], [100.0]])  # telha cinzenta
    gx, gy = _grad(rgb.mean(0), 1.0)
    patch = Patch(BOUNDS, gx, gy, patch.h, patch.has, patch.ndvi, edge_scale(gx, gy), rgb)
    assert rowcol(patch.transform, 14, 9) is not None
    out = split_rows(roof, patch)
    assert len(out) == 2
    xs = sorted(p.bounds[2] for p in out)
    assert abs(xs[0] - 14.0) <= 0.5


def test_split_rows_keeps_uniform_roof() -> None:
    from osm3d.reconstruction.ortho_outline import split_rows

    roof = box(2, 4, 26, 14)
    patch = _patch([(roof, 200, 5.0)])
    rows, _ = np.indices(patch.h.shape)
    y = BOUNDS[3] - (rows + 0.5) * RES_M
    patch.h[patch.has] = (6.0 - 0.6 * np.abs(y - 9.0))[patch.has]
    patch.rgb = np.stack([np.where(patch.has, 200.0, 60.0)] * 3)
    assert split_rows(roof, patch) == [roof]


def test_new_building_with_lean_to_exports_parts_sharing_nodes(tmp_path: Path) -> None:
    from osm3d.domain.models import RoofShape
    from osm3d.export.osm_xml import plan_export
    from osm3d.export.s3db_tags import TagPolicy
    from osm3d.parts import layout_from_polygons
    from tests.conftest import make_part

    src = tmp_path / "area.osm"
    src.write_text(OSM_XML, encoding="utf-8")
    o = np.array([-45000.0, 182000.0])
    main = shapely.affinity.translate(box(4, 0, 14, 20), *o)
    lean = shapely.affinity.translate(box(0, 4, 4, 14), *o)  # alpendre a oeste
    outline = shapely.union_all([main, lean])
    layout = layout_from_polygons([main, lean], source="suggested")
    parts = [make_part(RoofShape.GABLED), make_part(RoofShape.SKILLION)]
    b = Building(
        osm_type="new",
        osm_id=1,
        osm_version=0,
        footprint=outline,
        existing_tags={"building": "yes"},
        layout=layout,
        parts=parts,
        review_status="accepted",
    )
    selected, records = plan_export([b], TagPolicy())
    assert records[0].exported and "2 partes" in records[0].reason
    root = ET.parse(write_josm_file(selected, src, tmp_path / "out.osm")).getroot()
    ways = root.findall("way")
    assert len(ways) == 3
    kinds = [{t.attrib["k"] for t in w.findall("tag")} for w in ways]
    assert sum("building:part" in k for k in kinds) == 2
    refs = [{nd.attrib["ref"] for nd in w.findall("nd")} for w in ways]
    outline_refs = next(r for r, k in zip(refs, kinds, strict=True) if "building" in k)
    for r, k in zip(refs, kinds, strict=True):
        if "building:part" in k:
            assert len(r & outline_refs) >= 3  # parte presa ao contorno
