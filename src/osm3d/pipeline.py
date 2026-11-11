"""Fases do pipeline e respetivos artefactos.

Cada fase lê o artefacto da anterior e escreve o seu em
``data/processed/<área>/<NN>_<fase>.*``, para poder ser re-executada isoladamente
(p.ex. voltar a ajustar telhados sem voltar a ler o LiDAR).
"""

from __future__ import annotations

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


def run_stage(config: Config, stage: Stage, *, force: bool = False) -> Path:
    """Executa uma fase e devolve o caminho do artefacto produzido."""
    out = artefact_path(config, stage)
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
        case _:
            raise NotImplementedError(f"Fase '{stage.value}' ainda não implementada")
