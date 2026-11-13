"""Pipeline em segundo plano para a aplicação (uma corrida de cada vez).

Corre as fases de ``fetch`` a ``qa`` (e, opcionalmente, ``candidates``) para uma área,
numa thread, com o mesmo código da CLI (``run_stage``). Só corre as fases em falta ou
desatualizadas (``pipeline.stage_state``), salvo pedido explícito para refazer a partir
de uma fase. Cancelar só tem efeito entre fases: uma fase em curso (p.ex. o ajuste dos
telhados) vai até ao fim.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from osm3d.config import Config
from osm3d.pipeline import (
    STAGE_ORDER,
    Stage,
    StageState,
    clear_municipal_cache,
    run_stage,
    stage_state,
    with_municipal,
)

log = logging.getLogger(__name__)

MAX_LOG_LINES = 400
DEFAULT_STAGES: list[Stage] = STAGE_ORDER[: STAGE_ORDER.index(Stage.QA) + 1]

Runner = Callable[..., Any]
StateOf = Callable[[Config, Stage], tuple[StageState, str]]


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class PipelineJob:
    def __init__(
        self,
        on_done: Callable[[Config, bool], None] | None = None,
        runner: Runner = run_stage,
        state_of: StateOf = stage_state,
    ) -> None:
        # on_done(config, sucesso): p.ex. recarregar a revisão da área processada.
        self.on_done = on_done
        self._runner = runner
        self._state_of = state_of
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._cancel = threading.Event()
        self._state: dict[str, Any] = {"running": False, "area": None, "stages": [], "log": []}

    @property
    def running(self) -> bool:
        return self._thread is not None

    def state(self) -> dict[str, Any]:
        with self._lock:
            s = dict(self._state)
            s["stages"] = [dict(x) for x in s["stages"]]
            s["log"] = list(s["log"])
            s["running"] = self.running
            return s

    def _log(self, line: str) -> None:
        with self._lock:
            lines = self._state["log"]
            lines.append(line)
            del lines[:-MAX_LOG_LINES]

    def start(
        self,
        cfg: Config,
        *,
        candidates: bool = False,
        redo_from: Stage | None = None,
        municipal: bool | None = None,
        refresh_municipal: bool = False,
    ) -> dict[str, Any]:
        """Processa a área: só as fases em falta ou desatualizadas.

        ``redo_from``: refazer também essa fase e as seguintes, mesmo que estejam feitas
        (``fetch`` volta a pedir o OSM ao Overpass). ``municipal``: usar (ou não) a
        cartografia municipal nos candidatos (``None``: o do config.yaml);
        ``refresh_municipal``: voltar a obtê-la (refaz os candidatos).
        """
        stages = [*DEFAULT_STAGES, *([Stage.CANDIDATES] if candidates else [])]
        if redo_from is not None and redo_from not in stages:
            raise ValueError(f"Fase '{redo_from.value}' não faz parte deste processamento")
        if refresh_municipal and not candidates:
            raise ValueError("A cartografia municipal só é usada na fase dos candidatos")
        if municipal is not None:
            cfg = with_municipal(cfg, municipal)
        if refresh_municipal and not cfg.sources.municipal.enabled:
            raise ValueError("Ativar a cartografia municipal para a voltar a obter")
        with self._lock:
            if self._thread is not None:
                raise RuntimeError("Já há um processamento em curso")
            self._cancel.clear()
            self._state = {
                "running": True,
                "area": cfg.area.name,
                "stages": [{"stage": s.value, "status": "pending"} for s in stages],
                "current": None,
                "log": [],
                "error": None,
                "ok": None,
                "started_at": _now(),
                "finished_at": None,
            }
            self._thread = threading.Thread(
                target=self._run,
                args=(cfg, stages, redo_from, refresh_municipal),
                name="pipeline",
                daemon=True,
            )
            self._thread.start()
        return self.state()

    def cancel(self) -> dict[str, Any]:
        if self.running:
            self._cancel.set()
            self._log("Cancelamento pedido: termina no fim da fase em curso.")
        return self.state()

    def wait(self, timeout: float | None = None) -> bool:
        thread = self._thread
        if thread is not None:
            thread.join(timeout)
        return self._thread is None

    def _set_stage(self, i: int, status: str) -> None:
        with self._lock:
            self._state["stages"][i]["status"] = status
            self._state["current"] = (
                self._state["stages"][i]["stage"] if status == "running" else None
            )

    def _run(
        self,
        cfg: Config,
        stages: list[Stage],
        redo_from: Stage | None,
        refresh_municipal: bool = False,
    ) -> None:
        ok = False
        redo_idx = STAGE_ORDER.index(redo_from) if redo_from is not None else None
        chain_ran = False  # uma fase da cadeia correu: as seguintes têm de correr
        try:
            for i, stage in enumerate(stages):
                if self._cancel.is_set():
                    for j in range(i, len(stages)):
                        self._set_stage(j, "cancelled")
                    self._log("Processamento cancelado.")
                    break
                forced = redo_idx is not None and STAGE_ORDER.index(stage) >= redo_idx
                if stage is Stage.CANDIDATES and refresh_municipal:
                    clear_municipal_cache(cfg)
                    self._log("[candidates] cartografia municipal: a obter de novo")
                    forced = True
                # Os candidatos não dependem da cadeia fetch → qa (leem o LAZ/MDT e o seu
                # próprio contexto OSM): decidem pelo próprio estado.
                follows = chain_ran and stage is not Stage.CANDIDATES
                if not forced and not follows:
                    state, why = self._state_of(cfg, stage)
                    if state == "done":
                        self._set_stage(i, "cached")
                        self._log(f"[{stage.value}] já feita: mantida")
                        continue
                    self._log(f"[{stage.value}] {why or state}: a processar")
                self._set_stage(i, "running")
                self._log(f"[{stage.value}] …")
                # Só fetch e candidates têm cache própria do OSM: refeita se pedido.
                force = redo_from is Stage.FETCH and stage in (Stage.FETCH, Stage.CANDIDATES)
                out = self._runner(cfg, stage, force=force, progress=self._log)
                self._log(f"[{stage.value}] → {out}")
                self._set_stage(i, "done")
                chain_ran = chain_ran or stage is not Stage.CANDIDATES
            else:
                ok = True
                self._log("Processamento concluído.")
        except Exception as exc:  # erro de uma fase: fica registado e a corrida termina
            log.exception("Pipeline falhou")
            with self._lock:
                for s in self._state["stages"]:
                    if s["status"] in ("running", "pending"):
                        s["status"] = "error" if s["status"] == "running" else "skipped"
                self._state["error"] = f"{type(exc).__name__}: {exc}"
            self._log(f"Erro: {exc}")
        finally:
            with self._lock:
                self._state["ok"] = ok
                self._state["current"] = None
                self._state["finished_at"] = _now()
            if self.on_done:
                try:
                    self.on_done(cfg, ok)
                except Exception:
                    log.exception("Falha ao recarregar após o processamento")
            with self._lock:
                self._thread = None
