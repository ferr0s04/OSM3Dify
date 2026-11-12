"""Estado partilhado da aplicação: área ativa, revisão, dados raw e processamento.

A área ativa pode mudar com a aplicação a correr (separador "Dados"). Ao mudar, ou no
fim de um processamento dessa área, a revisão é recarregada a partir dos resultados.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Any

from shapely.geometry import shape

from osm3d.acquisition.inventory import REQUIRED_PRODUCTS, by_tile, has_product, scan_local
from osm3d.acquisition.osm import relation_boundary
from osm3d.app.data import DataService
from osm3d.app.downloads import DownloadManager
from osm3d.app.processing import PipelineJob
from osm3d.app.service import ReviewService
from osm3d.areas import area_store, with_area
from osm3d.config import AreaConfig, Config
from osm3d.geospatial.tiling import tiles_in_geometry
from osm3d.pipeline import Stage, artefact_path

# relation_id → (nome, polígono GeoJSON WGS84)
RelationLookup = Callable[[int], tuple[str | None, Any]]


class AppContext:
    def __init__(
        self,
        base: Config,
        service: ReviewService | None = None,
        downloads: DownloadManager | None = None,
        pipeline: PipelineJob | None = None,
        relation_lookup: RelationLookup | None = None,
    ) -> None:
        # ``base``: configuração do config.yaml; a sua área é a área inicial do registo.
        self.base = base
        self.store = area_store(base)
        self._lock = threading.RLock()
        self._lookup: RelationLookup = relation_lookup or relation_boundary
        self.cfg = service.cfg if service is not None else with_area(base, self.store.active())
        self.data = DataService(self.cfg, downloads)
        self.data.downloads.on_change = self._files_changed
        self.pipeline = pipeline or PipelineJob()
        self.pipeline.on_done = self._processed
        self.svc: ReviewService | None = service
        self.review_error: str | None = None
        # Muda sempre que a revisão é recarregada (outra área ou novos resultados): o
        # frontend compara-o para saber que tem de recarregar os edifícios.
        self.review_generation = 0
        if service is None:
            self._load_review()

    # ---------------------------------------------------------------- revisão

    def _load_review(self) -> None:
        try:
            self.svc, self.review_error = ReviewService(self.cfg), None
        except FileNotFoundError as exc:
            self.svc, self.review_error = None, str(exc)
        self.review_generation += 1

    def _files_changed(self) -> None:
        # LAZ descarregados ou apagados: a vista 3D passa a usar o índice atualizado.
        if self.svc is not None:
            self.svc.refresh_points()

    def _processed(self, cfg: Config, ok: bool) -> None:
        with self._lock:
            if cfg.area.name == self.cfg.area.name:
                self._load_review()

    # ------------------------------------------------------------------ áreas

    def _use(self, area: AreaConfig) -> None:
        self.cfg = with_area(self.base, area)
        self.data.cfg = self.cfg
        self._load_review()

    def _check_idle(self) -> None:
        if self.pipeline.running:
            area = self.pipeline.state()["area"]
            raise RuntimeError(f"Processamento em curso ({area}): esperar ou cancelar primeiro")

    def area_info(self, area: AreaConfig, local: dict[str, Any]) -> dict[str, Any]:
        qa = artefact_path(with_area(self.base, area), Stage.QA)
        processed_at = qa.stat().st_mtime if qa.exists() else None
        changed_at = self.store.tiles_changed_at(area.name)
        complete = [
            t
            for t in area.tiles
            if all(has_product(local.get(t, []), p) for p in REQUIRED_PRODUCTS)
        ]
        return {
            **area.model_dump(mode="json"),
            "bbox": list(area.bbox),
            "active": area.name == self.cfg.area.name,
            "from_config": self.store.is_default(area.name),
            "processed": processed_at is not None,
            "processed_at": processed_at,
            # Quadrículas alteradas depois do último processamento: voltar a processar.
            "stale": bool(processed_at and changed_at and changed_at > processed_at),
            "tiles_complete": complete,
        }

    def areas(self) -> dict[str, Any]:
        local = by_tile(scan_local(self.cfg.sources))
        return {
            "active": self.cfg.area.name,
            "areas": [self.area_info(a, local) for a in self.store.areas().values()],
            "review_available": self.svc is not None,
            "review_error": self.review_error,
            "review_generation": self.review_generation,
            "processing": self.pipeline.state(),
        }

    def activate(self, name: str) -> dict[str, Any]:
        with self._lock:
            self._check_idle()
            self._use(self.store.activate(name))
        return self.areas()

    def save_area(
        self,
        name: str | None,
        label: str | None,
        tiles: list[str],
        *,
        create: bool,
        activate: bool = False,
    ) -> dict[str, Any]:
        """Cria (id gerado, ``area-<n>``) ou altera uma área. O id nunca muda."""
        with self._lock:
            if create:
                area = AreaConfig(name="area-0", label=label, tiles=tiles)  # valida primeiro
                if activate:
                    self._check_idle()
                area = self.store.create(area.tiles, area.label, activate=activate)
                if activate:
                    self._use(area)
                return self.areas()
            if name not in self.store.areas():
                raise KeyError(name)
            area = AreaConfig(name=name, label=label or None, tiles=tiles)
            is_active = name == self.cfg.area.name
            if is_active or activate:
                self._check_idle()
            self.store.save(area, activate=activate)
            if is_active or activate:
                self._use(area)
        return self.areas()

    def delete_area(self, name: str) -> dict[str, Any]:
        with self._lock:
            if name == self.cfg.area.name:
                raise ValueError("Não se pode remover a área ativa: ativar outra primeiro")
            self.store.delete(name)
        return self.areas()

    def lookup_relation(self, relation_id: int) -> dict[str, Any]:
        """Quadrículas que cobrem um limite OSM (ajuda a escolher as de uma área)."""
        label, geojson = self._lookup(relation_id)
        keys = tiles_in_geometry(shape(geojson), self.base.processing_crs)
        tiles = [k.dgt_name for k in keys if k.dgt_name in self.data.index]
        if not tiles:
            raise ValueError(f"A relação {relation_id} não cobre quadrículas LiDAR da DGT")
        return {"label": label, "osm_relation_id": relation_id, "tiles": tiles}

    # ------------------------------------------------------------ processamento

    def process(self, *, candidates: bool = False, refresh_osm: bool = False) -> dict[str, Any]:
        with self._lock:
            return self.pipeline.start(self.cfg, candidates=candidates, refresh_osm=refresh_osm)
