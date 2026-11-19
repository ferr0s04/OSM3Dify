from __future__ import annotations

from pathlib import Path

from osm3d.config import load_config
from osm3d.pipeline import Stage, artefact_path

ROOT = Path(__file__).resolve().parents[1]


def test_default_config_is_valid() -> None:
    # Só o config.yaml versionado (sem o local.yaml desta máquina): é geral.
    cfg = load_config(ROOT / "config" / "config.yaml", local=False)
    assert cfg.processing_crs == "EPSG:3763"
    assert not cfg.area.tiles and not cfg.sources.municipal.enabled
    assert not cfg.export.overwrite_existing_tags
    assert artefact_path(cfg, Stage.ROOFS).name == "04_roofs.gpkg"


def test_local_yaml_overrides(tmp_path: Path) -> None:
    (tmp_path / "config.yaml").write_text("export:\n  height_rounding_m: 0.5\n", encoding="utf-8")
    (tmp_path / "local.yaml").write_text(
        "area:\n  name: area-7\n  tiles: [155480]\nexport:\n  roof_colour: false\n",
        encoding="utf-8",
    )
    cfg = load_config(tmp_path / "config.yaml")
    assert cfg.area.name == "area-7" and cfg.area.tiles == ["155480"]
    assert cfg.export.roof_colour is False and cfg.export.height_rounding_m == 0.5
    assert load_config(tmp_path / "config.yaml", local=False).area.tiles == []
