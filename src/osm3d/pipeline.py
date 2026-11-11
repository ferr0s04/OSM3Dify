"""Fases do pipeline e respetivos artefactos.

Cada fase lê o artefacto da anterior e escreve o seu em
``data/processed/<área>/<NN>_<fase>.*``, para poder ser re-executada isoladamente
(p.ex. voltar a ajustar telhados sem voltar a ler o LiDAR).
"""

from __future__ import annotations

from collections.abc import Callable
from enum import StrEnum
from pathlib import Path

from osm3d.config import Config


class Stage(StrEnum):
    FETCH = "fetch"
    PREPARE = "prepare"
    HEIGHTS = "heights"
    PARTS = "parts"
    ROOFS = "roofs"
    COLOURS = "colours"
    QA = "qa"
    EXPORT = "export"


STAGE_ORDER: list[Stage] = list(Stage)

_ARTEFACT_SUFFIX: dict[Stage, str] = {Stage.FETCH: ".osm", Stage.EXPORT: ".osm"}


def area_dir(config: Config) -> Path:
    return config.paths.processed / config.area.name


def artefact_path(config: Config, stage: Stage) -> Path:
    index = STAGE_ORDER.index(stage)
    suffix = _ARTEFACT_SUFFIX.get(stage, ".gpkg")
    base = config.paths.output if stage is Stage.EXPORT else area_dir(config)
    return base / f"{index:02d}_{stage.value}{suffix}"


# Fases ainda sem processamento próprio: passam o artefacto anterior adiante.
# HEIGHTS: integrada em ROOFS (a altura vem do topo do modelo de telhado).
# PARTS: Fase 4 — por agora, uma parte por edifício.
# COLOURS: Fase 5 — desligada em config (appearance.enabled).
_PASSTHROUGH = {Stage.HEIGHTS, Stage.PARTS, Stage.COLOURS}


def _require(path: Path, stage: Stage) -> Path:
    if not path.exists():
        raise FileNotFoundError(f"Falta o artefacto {path}: correr primeiro a fase '{stage.value}'")
    return path


def _input_of(config: Config, stage: Stage) -> Path:
    """Artefacto de entrada: o da fase anterior, saltando fases de passagem."""
    idx = STAGE_ORDER.index(stage) - 1
    while STAGE_ORDER[idx] in _PASSTHROUGH:
        idx -= 1
    prev = STAGE_ORDER[idx]
    return _require(artefact_path(config, prev), prev)


def run_stage(
    config: Config,
    stage: Stage,
    *,
    force: bool = False,
    progress: Callable[[str], None] | None = None,
) -> Path:
    """Executa uma fase e devolve o caminho do artefacto produzido."""
    out = artefact_path(config, stage)
    if stage in _PASSTHROUGH:
        return _input_of(config, stage)

    match stage:
        case Stage.FETCH:
            from osm3d.acquisition.osm import fetch_buildings

            return fetch_buildings(
                config.area.bbox,
                config.sources.overpass_url,
                out,
                config.area.osm_relation_id,
                force=force,
            )
        case Stage.PREPARE:
            from osm3d.export.geopackage import write_buildings
            from osm3d.geospatial.footprints import load_footprints

            buildings = load_footprints(
                _input_of(config, stage), config.processing_crs, config.footprints.min_area_m2
            )
            return write_buildings(buildings, out, config.processing_crs)
        case Stage.ROOFS:
            from osm3d.export.geopackage import read_buildings, write_buildings
            from osm3d.geospatial.point_clouds import index_tiles
            from osm3d.reconstruction.building import reconstruct_area

            buildings = read_buildings(_input_of(config, stage))
            lidar = index_tiles(config.sources.lidar_dir, (".laz", ".las"))
            dtm = index_tiles(config.sources.dtm_dir, (".tif", ".tiff"))
            if progress:
                progress(f"  quadrículas com LAZ+MDT: {', '.join(sorted(set(lidar) & set(dtm)))}")
            result, _ = reconstruct_area(buildings, lidar, dtm, config, progress)
            return write_buildings(result, out, config.processing_crs)
        case Stage.QA:
            from osm3d.export.geopackage import read_buildings, write_buildings
            from osm3d.validation.quality import check_building

            checked = []
            for b in read_buildings(_input_of(config, stage)):
                known = {f.code for f in b.qa_flags}
                extra = [f for f in check_building(b) if f.code not in known]
                checked.append(b.model_copy(update={"qa_flags": [*b.qa_flags, *extra]}))
            return write_buildings(checked, out, config.processing_crs)
        case _:
            raise NotImplementedError(f"Fase '{stage.value}' ainda não implementada")
