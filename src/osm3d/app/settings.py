"""Opções da aplicação que se mudam no frontend e ficam guardadas entre sessões.

Ficam em ``data/app_settings.json`` (ao lado de ``areas.json``) e sobrepõem-se às do
``config.yaml`` só na aplicação. ``None`` = usar o valor do ``config.yaml``.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from pydantic import BaseModel, ValidationError

from osm3d.config import Config

log = logging.getLogger(__name__)


class AppSettings(BaseModel):
    # Acrescentar/alterar roof:colour nas tags propostas e exportadas.
    export_roof_colour: bool | None = None


def settings_path(cfg: Config) -> Path:
    return cfg.paths.areas_path.with_name("app_settings.json")


def load_settings(cfg: Config) -> AppSettings:
    path = settings_path(cfg)
    if not path.exists():
        return AppSettings()
    try:
        return AppSettings.model_validate_json(path.read_text(encoding="utf-8"))
    except (ValueError, ValidationError) as exc:
        log.warning("Opções da aplicação ilegíveis (%s): a usar as do config.yaml", exc)
        return AppSettings()


def save_settings(cfg: Config, settings: AppSettings) -> None:
    path = settings_path(cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".part")
    tmp.write_text(json.dumps(settings.model_dump(), indent=2), encoding="utf-8")
    tmp.replace(path)


def apply_settings(cfg: Config, settings: AppSettings) -> Config:
    """``cfg`` com as opções da aplicação aplicadas."""
    if settings.export_roof_colour is None:
        return cfg
    export = cfg.export.model_copy(update={"roof_colour": settings.export_roof_colour})
    return cfg.model_copy(update={"export": export})
