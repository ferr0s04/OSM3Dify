"""Execução em paralelo (vários processos) de tarefas independentes e determinísticas.

O ajuste dos telhados e a deteção de candidatos são CPU puro (numpy em ciclos curtos),
independentes por edifício ou por quadrícula: em processos separados dão exatamente os
mesmos resultados, pela mesma ordem, em menos tempo.

Cada processo abre os seus próprios recursos (MDT, ortofotos) num ``initializer``:
datasets rasterio não passam entre processos. Se o pool não arrancar (p.ex. ambiente
sem ``spawn``), corre-se em série, com o mesmo resultado.
"""

from __future__ import annotations

import logging
import multiprocessing
import os
import pickle
from collections.abc import Callable, Iterator, Sequence
from concurrent.futures import Executor, ProcessPoolExecutor
from concurrent.futures.process import BrokenProcessPool
from contextlib import contextmanager
from typing import Any

log = logging.getLogger(__name__)

# Abaixo disto o arranque dos processos (~2 s cada, a importar numpy/shapely) não
# compensa.
MIN_PARALLEL_TASKS = 24
MAX_AUTO_WORKERS = 8


def worker_count(configured: int | None, n_tasks: int, min_tasks: int = MIN_PARALLEL_TASKS) -> int:
    """N.º de processos: o configurado, ou núcleos − 1 (máx. 8); 1 = em série.

    ``min_tasks``: abaixo disto corre em série (tarefas curtas não pagam o arranque).
    """
    if n_tasks < min_tasks:
        return 1
    if configured is None:
        configured = min(MAX_AUTO_WORKERS, max(1, (os.cpu_count() or 1) - 1))
    return max(1, min(configured, n_tasks))


@contextmanager
def process_pool(
    workers: int,
    initializer: Callable[..., None] | None = None,
    initargs: tuple[Any, ...] = (),
) -> Iterator[Executor | None]:
    """Pool de processos (``None`` com 1 processo: o chamador corre em série)."""
    if workers <= 1:
        yield None
        return
    pool = ProcessPoolExecutor(
        max_workers=workers,
        mp_context=multiprocessing.get_context("spawn"),
        initializer=initializer,
        initargs=initargs,
    )
    try:
        yield pool
    finally:
        pool.shutdown(wait=True, cancel_futures=True)


def ordered_map[T, R](
    pool: Executor | None,
    fn: Callable[[T], R],
    items: Sequence[T],
    serial: Callable[[T], R] | None = None,
) -> list[R]:
    """``[fn(x) for x in items]``, em paralelo se houver pool (a ordem mantém-se).

    ``serial``: a mesma função a correr neste processo (com os recursos já abertos
    aqui), usada sem pool ou se o pool falhar.
    """
    run_here = serial or fn
    if pool is None or len(items) < 2:
        return [run_here(x) for x in items]
    workers = getattr(pool, "_max_workers", 1)
    chunk = max(1, len(items) // (workers * 4))
    try:
        return list(pool.map(fn, items, chunksize=chunk))
    except (BrokenProcessPool, OSError, pickle.PicklingError) as exc:
        log.warning("Processamento paralelo falhou (%s): a correr em série", exc)
        return [run_here(x) for x in items]
