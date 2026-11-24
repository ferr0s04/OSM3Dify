"""Download de toda a cartografia municipal do concelho, em segundo plano.

Uma tarefa de cada vez, numa thread (como os downloads da DGT). A cópia fica em
``MunicipalSourceConfig.full_path`` e é partilhada por todas as áreas: a fase candidates
recorta-a localmente e deixa de pedir ao geoportal.
"""

from __future__ import annotations

import logging
import threading
import urllib.error
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from osm3d.acquisition.municipal import (
    MapGuideClient,
    MunicipalCancelled,
    MunicipalError,
    fetch_full_municipality,
)
from osm3d.config import MunicipalSourceConfig

log = logging.getLogger(__name__)

# (configuração, on_box, cancelar, progresso) → caminho; substituível nos testes.
Fetcher = Callable[..., Any]


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class MunicipalDownload:
    def __init__(self, fetcher: Fetcher | None = None) -> None:
        self._fetch = fetcher or self._default_fetch
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._cancel = threading.Event()
        self._state: dict[str, Any] = {"running": False}
        # Chamado no fim de um download bem-sucedido (p.ex. atualizar o estado das fases).
        self.on_done: Callable[[], None] | None = None

    @staticmethod
    def _default_fetch(cfg: MunicipalSourceConfig, **kwargs: Any) -> Any:
        return fetch_full_municipality(cfg, MapGuideClient(cfg), **kwargs)

    @property
    def running(self) -> bool:
        return self._thread is not None

    def state(self) -> dict[str, Any]:
        with self._lock:
            return {**self._state, "running": self.running}

    def start(self, cfg: MunicipalSourceConfig) -> dict[str, Any]:
        with self._lock:
            if self._thread is not None:
                raise RuntimeError("O download da cartografia municipal já está a decorrer")
            self._cancel.clear()
            self._state = {
                "running": True,
                "done": 0,
                "total": None,
                "found": 0,
                "message": "a pedir a extensão do concelho ao geoportal…",
                "error": None,
                "ok": None,
                "started_at": _now(),
                "finished_at": None,
            }
            self._thread = threading.Thread(
                target=self._run, args=(cfg,), name="municipal-download", daemon=True
            )
            self._thread.start()
        return self.state()

    def cancel(self) -> dict[str, Any]:
        if self.running:
            self._cancel.set()
            with self._lock:
                self._state["message"] = "a cancelar no fim do pedido em curso…"
        return self.state()

    def wait(self, timeout: float | None = None) -> bool:
        thread = self._thread
        if thread is not None:
            thread.join(timeout)
        return self._thread is None

    def _on_box(self, done: int, total: int, found: int) -> None:
        with self._lock:
            self._state.update(
                done=done,
                total=total,
                found=found,
                message=f"{done} de {total} pedidos · {found:,} elementos".replace(",", " "),
            )

    def _progress(self, line: str) -> None:
        with self._lock:
            self._state["message"] = line.strip()

    def _run(self, cfg: MunicipalSourceConfig) -> None:
        ok = False
        try:
            path = self._fetch(
                cfg, on_box=self._on_box, cancel=self._cancel, progress=self._progress
            )
            ok = True
            with self._lock:
                self._state["message"] = (
                    f"{self._state.get('found', 0):,} elementos guardados em {path}".replace(
                        ",", " "
                    )
                )
        except MunicipalCancelled:
            with self._lock:
                self._state["message"] = (
                    "cancelado: os pedidos feitos ficam guardados (o download seguinte retoma)"
                )
        except (MunicipalError, urllib.error.URLError, OSError, ValueError) as exc:
            log.warning("Download da cartografia municipal falhou: %s", exc)
            with self._lock:
                self._state["error"] = f"{type(exc).__name__}: {exc}"
                self._state["message"] = (
                    "falhou: os pedidos feitos ficam guardados (o download seguinte retoma)"
                )
        finally:
            with self._lock:
                self._state["ok"] = ok
                self._state["finished_at"] = _now()
            if ok and self.on_done:
                try:
                    self.on_done()
                except Exception:
                    log.exception("Falha depois do download da cartografia municipal")
            with self._lock:
                self._thread = None
