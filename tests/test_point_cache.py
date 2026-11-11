from __future__ import annotations

from pathlib import Path

import numpy as np

from osm3d.app.points import TilePointCache, _load, _save
from osm3d.geospatial.point_clouds import PointSet


def test_cache_roundtrip_is_compact_and_precise(tmp_path: Path) -> None:
    rng = np.random.default_rng(0)
    n = 10_000
    xyz = np.column_stack(
        [rng.uniform(-45_000, -44_000, n), rng.uniform(179_000, 180_000, n), rng.uniform(30, 80, n)]
    )
    ps = PointSet(xyz, np.full(n, 6, np.uint8), rng.integers(0, 256, (n, 3)).astype(np.uint16))
    path = tmp_path / "t.npz"
    _save(path, ps)
    back = _load(path)
    assert back is not None
    assert np.abs(back.xyz - xyz).max() < 1e-3  # float32 relativo: sub-milimétrico
    assert np.array_equal(back.rgb, ps.rgb)
    assert path.stat().st_size < n * 20  # vs. 40 B/ponto no formato antigo


def test_old_format_is_rebuilt(tmp_path: Path) -> None:
    path = tmp_path / "old.npz"
    np.savez(path, xyz=np.zeros((1, 3)), cls=np.zeros(1), rgb=np.zeros((1, 3)))
    assert _load(path) is None


def test_lru_limit(tmp_path: Path) -> None:
    cache = TilePointCache({}, tmp_path, max_in_memory=2)
    empty = PointSet.empty()
    for name in ("a", "b", "c"):
        cache.lidar_tiles[name] = tmp_path / f"{name}.laz"
        _save(tmp_path / f"{name}.npz", empty)
        cache._tile(name)
    assert list(cache._mem) == ["b", "c"]
