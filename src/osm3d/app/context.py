"""Estado partilhado da aplicação: área ativa, revisão, dados raw e processamento.

A área ativa pode mudar com a aplicação a correr (separador "Dados"). Ao mudar, ou no
fim de um processamento dessa área, a revisão é recarregada a partir dos resultados.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from typing import Any

from shapely.geometry import shape

from osm3d.acquisition.inventory import REQUIRED_PRODUCTS, by_tile, has_product, scan_local
from osm3d.acquisition.osm import relation_boundary
from osm3d.app.data import DataService
from osm3d.app.downloads import DownloadManager
from osm3d.app.municipal import MunicipalDownload
from osm3d.app.processing import DEFAULT_STAGES, PipelineJob
from osm3d.app.service import ReviewService
from osm3d.app.settings import AppSettings, apply_settings, load_settings, save_settings
from osm3d.areas import area_store, with_area
from osm3d.config import AreaConfig, Config
from osm3d.geospatial.tiling import tiles_in_geometry
from osm3d.pipeline import Stage, artefact_path, municipal_status, stage_state

log = logging.getLogger(__name__)

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
        municipal: MunicipalDownload | None = None,
    ) -> None:
        # ``base``: configuração do config.yaml (com as opções guardadas da aplicação); a
        # sua área é a área inicial do registo.
        self.yaml_base = base
        self.settings = load_settings(base)
        base = apply_settings(base, self.settings)
        self.base = base
        self.store = area_store(base)
        self._lock = threading.RLock()
        self._lookup: RelationLookup = relation_lookup or relation_boundary
        self.cfg = (
            apply_settings(service.cfg, self.settings)
            if service is not None
            else with_area(base, self.store.active())
        )
        if service is not None and service.cfg != self.cfg:
            service = ReviewService(self.cfg)
        self.data = DataService(self.cfg, downloads)
        self.data.downloads.on_change = self._files_changed
        self.pipeline = pipeline or PipelineJob()
        self.pipeline.on_done = self._processed
        self.svc: ReviewService | None = service
        # Cópia de todo o concelho (cartografia municipal), descarregada a pedido.
        self.municipal = municipal or MunicipalDownload()
        self.review_error: str | None = None
        # Muda sempre que a revisão é recarregada (outra área ou novos resultados): o
        # frontend compara-o para saber que tem de recarregar os edifícios.
        self.review_generation = 0
        self._places_busy = False
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
            # Nome a mostrar: o escrito, ou as freguesias cobertas (calculadas à parte).
            "label": area.display_label,
            "label_manual": area.label,
            "bbox": list(area.bbox),
            "active": area.name == self.cfg.area.name,
            "from_config": self.store.is_default(area.name),
            "processed": processed_at is not None,
            "processed_at": processed_at,
            # Quadrículas alteradas depois do último processamento: voltar a processar.
            "stale": bool(processed_at and changed_at and changed_at > processed_at),
            "tiles_complete": complete,
        }

    def _update_places(self) -> None:
        """Calcula, em segundo plano, as freguesias das áreas que ainda não as têm."""
        todo = [a for a in self.store.areas().values() if a.tiles and not a.places_current]
        if not todo or self._places_busy:
            return
        self._places_busy = True

        def work() -> None:
            from osm3d.acquisition.places import area_places

            try:
                for a in todo:
                    try:
                        names = area_places(
                            a.tiles, self.base.processing_crs, self.base.sources.overpass_url
                        )
                    except Exception as exc:  # Overpass ocupado: tenta-se no próximo pedido
                        log.info("Freguesias de %s por calcular: %s", a.name, exc)
                        continue
                    self.store.set_places(a.name, a.tiles, names)
                    if a.name == self.cfg.area.name:
                        with self._lock:
                            self.cfg = with_area(self.base, self.store.get(a.name))
                            self.data.cfg = self.cfg
            finally:
                self._places_busy = False

        threading.Thread(target=work, name="area-places", daemon=True).start()

    def areas(self) -> dict[str, Any]:
        self._update_places()
        local = by_tile(scan_local(self.cfg.sources))
        return {
            "active": self.cfg.area.name,
            "areas": [self.area_info(a, local) for a in self.store.areas().values()],
            "review_available": self.svc is not None,
            "review_error": self.review_error,
            "review_generation": self.review_generation,
            "stages": self.stages(),
            "processing": self.pipeline.state(),
            # Cartografia municipal (fase candidates): estado da cache e se foi usada.
            "municipal": municipal_status(self.cfg) | {"download": self.municipal.state()},
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

    # ------------------------------------------------------- cartografia municipal

    def download_municipality(self) -> dict[str, Any]:
        """Descarrega (em segundo plano) todos os edifícios do concelho."""
        if not self.base.sources.municipal.configured:
            raise ValueError("Sem geoportal municipal configurado (sources.municipal)")
        return self.municipal.start(self.base.sources.municipal)

    # ------------------------------------------------------------------ opções

    def settings_info(self) -> dict[str, Any]:
        return {
            # Valor em uso (opção guardada, senão o do config.yaml) e o do config.yaml.
            "export_roof_colour": self.cfg.export.roof_colour,
            "config_export_roof_colour": self.yaml_base.export.roof_colour,
        }

    def update_settings(self, export_roof_colour: bool | None) -> dict[str, Any]:
        """Muda as opções da aplicação e recarrega a revisão com elas."""
        with self._lock:
            self.settings = AppSettings(export_roof_colour=export_roof_colour)
            save_settings(self.yaml_base, self.settings)
            self.base = apply_settings(self.yaml_base, self.settings)
            self.cfg = with_area(self.base, self.cfg.area)
            self.data.cfg = self.cfg
            self._load_review()
        return self.settings_info()

    # ------------------------------------------------------------ processamento

    def stages(self) -> list[dict[str, Any]]:
        """Estado das fases da área ativa (as de passagem não aparecem)."""
        out = []
        for stage in [*DEFAULT_STAGES, Stage.CANDIDATES]:
            state, why = stage_state(self.cfg, stage)
            if why == "sem processamento próprio":
                continue
            out.append({"stage": stage.value, "state": state, "reason": why})
        return out

    def process(
        self,
        *,
        candidates: bool = False,
        redo_from: Stage | None = None,
        municipal: bool | None = None,
        refresh_municipal: bool = False,
    ) -> dict[str, Any]:
        """Processa as fases em falta (e, se pedido, refaz a partir de ``redo_from``)."""
        if not self.cfg.area.tiles:
            raise ValueError("A área ativa não tem quadrículas: escolhê-las no mapa primeiro")
        with self._lock:
            return self.pipeline.start(
                self.cfg,
                candidates=candidates,
                redo_from=redo_from,
                municipal=municipal,
                refresh_municipal=refresh_municipal,
            )
