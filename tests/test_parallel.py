from __future__ import annotations

import math

from osm3d.parallel import MIN_PARALLEL_TASKS, ordered_map, process_pool, worker_count


def test_worker_count() -> None:
    assert worker_count(None, 3) == 1  # poucas tarefas: em série
    assert worker_count(4, 100) == 4
    assert worker_count(1, 100) == 1
    assert worker_count(8, 3, min_tasks=2) == 3
    assert worker_count(None, MIN_PARALLEL_TASKS) >= 1


def test_ordered_map_same_result_in_parallel() -> None:
    items = [float(i) for i in range(40)]
    expected = [math.sqrt(x) for x in items]
    with process_pool(1) as pool:
        assert pool is None
        assert ordered_map(pool, math.sqrt, items) == expected
    with process_pool(2) as pool:
        assert ordered_map(pool, math.sqrt, items) == expected
