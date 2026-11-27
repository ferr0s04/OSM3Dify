"""Cache dos pontos dos LAZ das fases: o mesmo resultado que ler o LAZ, e refeita se o LAZ muda."""

from __future__ import annotations

import os
from pathlib import Path

import laspy
import numpy as np
from shapely.geometry import box

from osm3d.geospatial import point_clouds as pc


def _write_las(path: Path, n: int = 2000, seed: int = 0) -> None:
    rng = np.random.default_rng(seed)
    header = laspy.LasHeader(point_format=7, version="1.4")  # com RGB
    header.scales = np.array([0.01, 0.01, 0.01])
    header.offsets = np.array([-45000.0, 183000.0, 0.0])
    las = laspy.LasData(header)
    las.x = rng.uniform(-45000, -44900, n)
    las.y = rng.uniform(183000, 183100, n)
    las.z = rng.uniform(90, 110, n)
    las.classification = rng.choice([2, 5, 6], n).astype(np.uint8)
    las.red = rng.integers(0, 65535, n, dtype=np.uint16)
    las.green = rng.integers(0, 65535, n, dtype=np.uint16)
    las.blue = rng.integers(0, 65535, n, dtype=np.uint16)
    las.write(path)


def _same(a: pc.PointSet, b: pc.PointSet) -> bool:
    return (
        np.array_equal(a.xyz, b.xyz)
        and np.array_equal(a.classification, b.classification)
        and a.rgb is not None
        and b.rgb is not None
        and np.array_equal(a.rgb, b.rgb)
    )


def test_cached_points_equal_the_laz_and_follow_changes(tmp_path: Path) -> None:
    las = tmp_path / "LO-154484-07-2025_v05.las"
    _write_las(las)
    cache = tmp_path / "cache"
    bbox = (-44980.0, 183020.0, -44950.0, 183070.0)
    pc._memo.clear()

    direct = pc.read_points([las], bbox, [6])
    cached = pc.read_points([las], bbox, [6], cache_dir=cache)
    assert len(direct) > 0 and _same(direct, cached)
    assert pc.point_cache_file(cache, las, [6]).exists()
    pc._memo.clear()
    assert _same(direct, pc.read_points([las], bbox, [6], cache_dir=cache))  # do disco

    # O LAZ mudou (descarregado de novo): a cache não serve e é refeita.
    _write_las(las, seed=1)
    os.utime(las, ns=(las.stat().st_atime_ns, las.stat().st_mtime_ns + 10**9))
    again = pc.read_points([las], bbox, [6], cache_dir=cache)
    assert _same(pc.read_points([las], bbox, [6]), again)
    assert not _same(direct, again)


def test_assign_points_strictly_inside(tmp_path: Path) -> None:
    xyz = np.array([[0.5, 0.5, 1], [1.0, 0.5, 1], [1.5, 0.5, 1], [2.5, 0.5, 1], [0.2, 0.9, 1]])
    pts = pc.PointSet(xyz, np.full(len(xyz), 6, np.uint8))
    a, b = pc.assign_points(pts, [box(0, 0, 1, 1), box(1, 0, 2, 1)])
    # (1.0, 0.5) está na aresta comum: em nenhum (como o "within" de antes).
    assert a.tolist() == [0, 4] and b.tolist() == [2]
    shifted = pc.assign_points(pts, [box(0, 0, 1, 1)], offset=(2.0, 0.0))
    assert shifted[0].tolist() == [3]
