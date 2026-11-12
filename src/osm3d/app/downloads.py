"""Downloads da DGT em segundo plano para a aplicação (uma thread, pedidos sequenciais).

Cada tarefa é um par (quadrícula, produto). Os downloads não correm dentro dos pedidos
HTTP: a API só acrescenta tarefas à fila e lê o estado. Uma só sessão DGT, pedidos em
série com pausa (uso responsável, como na CLI ``osm3d dgt``).
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

import requests

from osm3d.acquisition.dgt import (
    DEFAULT_STAC_URL,
    DgtAsset,
    DgtCancelled,
    DgtClient,
    DgtError,
    DgtItem,
    tile_items,
)
from osm3d.acquisition.inventory import PRODUCTS, by_tile, has_product, scan_local
from osm3d.config import Config
from osm3d.env import require_env
from osm3d.geospatial.tiling import TileKey

log = logging.getLogger(__name__)

TaskStatus = Literal[
    "queued", "searching", "downloading", "done", "exists", "missing", "error", "cancelled"
]
ACTIVE: frozenset[str] = frozenset({"queued", "searching", "downloading"})


class Client(Protocol):
    """O que o gestor usa do :class:`DgtClient` (substituível nos testes)."""

    def search(
        self, bbox_wgs84: tuple[float, float, float, float], collections: list[str]
    ) -> list[DgtItem]: ...

    def download(
        self,
        item: DgtItem,
        asset: DgtAsset,
        dest_dir: Any,
        *,
        on_bytes: Callable[[int, int], None] | None = None,
        cancel: threading.Event | None = None,
    ) -> Any: ...


ClientFactory = Callable[[], Client]


def dgt_client_factory(cfg: Config) -> ClientFactory:
    """Cliente DGT com sessão iniciada (credenciais DGT_USER/DGT_PASSWORD do .env)."""

    def make() -> Client:
        user, password = require_env("DGT_USER", "DGT_PASSWORD")
        client = DgtClient(
            user,
            password,
            cfg.sources.dgt_stac_url or DEFAULT_STAC_URL,
            delay_s=cfg.sources.dgt_delay_s,
        )
        client.login()
        return client

    return make


@dataclass
class Task:
    id: int
    tile: str
    product: str
    status: TaskStatus = "queued"
    message: str = ""
    done_bytes: int = 0
    total_bytes: int = 0
    files: list[str] = field(default_factory=list)
    cancel: threading.Event = field(default_factory=threading.Event, repr=False)

    def to_json(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "tile": self.tile,
            "product": self.product,
            "status": self.status,
            "message": self.message,
            "done_bytes": self.done_bytes,
            "total_bytes": self.total_bytes,
            "files": self.files,
        }


class DownloadManager:
    def __init__(
        self,
        cfg: Config,
        client_factory: ClientFactory | None = None,
        on_change: Callable[[], None] | None = None,
    ) -> None:
        self.cfg = cfg
        self._factory = client_factory or dgt_client_factory(cfg)
        # Chamado quando ficheiros locais mudam (p.ex. reindexar os LAZ da revisão).
        self.on_change = on_change
        self._client: Client | None = None
        self._tasks: list[Task] = []
        self._next_id = 1
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        # Incrementa sempre que os ficheiros locais mudam (o frontend recarrega então).
        self.version = 0

    # --------------------------------------------------------------- API pública

    @property
    def running(self) -> bool:
        return self._thread is not None

    def submit(
        self, tiles: Iterable[str], products: Iterable[str], *, skip_existing: bool = True
    ) -> dict[str, Any]:
        """Acrescenta à fila as combinações (quadrícula, produto) que faltam."""
        tiles = list(dict.fromkeys(tiles))
        products = list(dict.fromkeys(products))
        for t in tiles:
            TileKey.from_dgt_name(t)  # valida (ValueError → 422)
        unknown = set(products) - set(PRODUCTS)
        if unknown:
            raise ValueError(f"Produto desconhecido: {', '.join(sorted(unknown))}")
        if not tiles or not products:
            raise ValueError("Indicar pelo menos uma quadrícula e um produto")
        local = by_tile(scan_local(self.cfg.sources, products))
        with self._lock:
            if not self.running:
                self._tasks = []  # nova série: o painel mostra só esta
            active = {(t.tile, t.product) for t in self._tasks if t.status in ACTIVE}
            for tile in tiles:
                for product in products:
                    if (tile, product) in active:
                        continue
                    task = Task(self._next_id, tile, product)
                    self._next_id += 1
                    if skip_existing and has_product(local.get(tile, []), product):
                        task.status, task.message = "exists", "já descarregado"
                    self._tasks.append(task)
            if self._thread is None and any(t.status == "queued" for t in self._tasks):
                self._thread = threading.Thread(target=self._run, name="dgt-downloads", daemon=True)
                self._thread.start()
        return self.state()

    def cancel(self) -> dict[str, Any]:
        """Cancela o download em curso e os pendentes."""
        with self._lock:
            for t in self._tasks:
                if t.status in ACTIVE:
                    t.cancel.set()
                    if t.status == "queued":
                        t.status, t.message = "cancelled", "cancelado"
        return self.state()

    def busy_tiles(self) -> set[str]:
        with self._lock:
            return {t.tile for t in self._tasks if t.status in ACTIVE}

    def state(self) -> dict[str, Any]:
        with self._lock:
            tasks = [t.to_json() for t in self._tasks]
        counts: dict[str, int] = {}
        for t in tasks:
            counts[t["status"]] = counts.get(t["status"], 0) + 1
        return {"running": self.running, "version": self.version, "counts": counts, "tasks": tasks}

    def wait(self, timeout: float | None = None) -> bool:
        """Espera pelo fim da fila (para testes e para terminar). True se terminou."""
        thread = self._thread
        if thread is not None:
            thread.join(timeout)
        return self._thread is None

    def notify_changed(self) -> None:
        self.version += 1
        if self.on_change:
            try:
                self.on_change()
            except Exception:  # um erro aqui não deve parar os downloads
                log.exception("Falha ao atualizar após alteração de ficheiros")

    # -------------------------------------------------------------------- worker

    def _next(self) -> Task | None:
        with self._lock:
            task = next((t for t in self._tasks if t.status == "queued"), None)
            if task is None:
                self._thread = None
                return None
            task.status, task.message = "searching", "a pesquisar no portal DGT…"
            return task

    def _fail_all(self, message: str) -> None:
        with self._lock:
            for t in self._tasks:
                if t.status in ACTIVE:
                    t.status, t.message = "error", message

    def _run(self) -> None:
        while (task := self._next()) is not None:
            if task.cancel.is_set():
                task.status, task.message = "cancelled", "cancelado"
                continue
            try:
                if self._client is None:
                    self._client = self._factory()
            except (RuntimeError, requests.RequestException) as exc:
                # Credenciais em falta ou login recusado: não adianta tentar as restantes.
                self._fail_all(str(exc))
                continue
            try:
                self._process(self._client, task)
            except DgtCancelled:
                task.status, task.message = "cancelled", "cancelado"
            except (DgtError, requests.RequestException, OSError) as exc:
                log.warning("Download %s/%s falhou: %s", task.tile, task.product, exc)
                task.status, task.message = "error", str(exc)

    def _process(self, client: Client, task: Task) -> None:
        sources = self.cfg.sources
        collection = sources.dgt_collections[task.product]
        # Uma folha de ortofoto cobre ~40 quadrículas: se uma tarefa anterior já a
        # descarregou, esta quadrícula já está coberta (sem novo pedido à DGT).
        local = by_tile(scan_local(sources, [task.product]))
        if has_product(local.get(task.tile, []), task.product):
            task.status, task.message = "exists", "já coberta por um ficheiro descarregado"
            return
        key = TileKey.from_dgt_name(task.tile)
        items = tile_items(client, key, collection, self.cfg.processing_crs)
        assets = [(it, a) for it in items for a in it.assets]
        if not assets:
            task.status, task.message = "missing", f"sem dados em {collection}"
            return
        task.status = "downloading"
        last = 0.0

        def on_bytes(done: int, total: int) -> None:
            nonlocal last
            task.done_bytes, task.total_bytes = done, total
            if time.monotonic() - last > 0.5:
                last = time.monotonic()
                task.message = f"{done / 1e6:.0f} MB" + (
                    f" de {total / 1e6:.0f} MB" if total else ""
                )

        for item, asset in assets:
            task.message = f"{item.id} (v{item.version})"
            path = client.download(
                item,
                asset,
                sources.product_dir(task.product),
                on_bytes=on_bytes,
                cancel=task.cancel,
            )
            task.files.append(path.name)
            self.notify_changed()
        task.status, task.message = "done", ", ".join(task.files)
