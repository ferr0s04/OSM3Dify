from __future__ import annotations

from pathlib import Path

from osm3d.config import load_config
from osm3d.pipeline import Stage, artefact_path

ROOT = Path(__file__).resolve().parents[1]


def test_default_config_is_valid() -> None:
    cfg = load_config(ROOT / "config" / "config.yaml")
    assert cfg.processing_crs == "EPSG:3763"
    assert not cfg.export.overwrite_existing_tags
    assert artefact_path(cfg, Stage.ROOFS).name == "04_roofs.gpkg"
