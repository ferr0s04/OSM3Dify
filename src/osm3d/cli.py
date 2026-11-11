"""CLI ``osm3d``: um subcomando por fase, mais ``run`` para o pipeline completo."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from osm3d import __version__
from osm3d.config import Config, load_config
from osm3d.pipeline import STAGE_ORDER, Stage, artefact_path, run_stage

app = typer.Typer(help="Gera tags Simple 3D Buildings para edifícios OSM.", no_args_is_help=True)

ConfigOption = Annotated[
    Path, typer.Option("--config", "-c", help="Ficheiro YAML de configuração.")
]
DEFAULT_CONFIG = Path("config/config.yaml")


def _load(path: Path) -> Config:
    if not path.exists():
        raise typer.BadParameter(f"Configuração não encontrada: {path}")
    return load_config(path)


def _run(config: Config, stage: Stage) -> None:
    try:
        out = run_stage(config, stage)
    except NotImplementedError as exc:
        typer.secho(f"[{stage.value}] {exc}", fg=typer.colors.YELLOW, err=True)
        raise typer.Exit(code=2) from exc
    typer.secho(f"[{stage.value}] → {out}", fg=typer.colors.GREEN)


@app.command()
def version() -> None:
    """Mostra a versão."""
    typer.echo(__version__)


@app.command()
def info(config: ConfigOption = DEFAULT_CONFIG) -> None:
    """Mostra a configuração ativa e os artefactos de cada fase."""
    cfg = _load(config)
    typer.echo(f"Área: {cfg.area.name}  bbox={cfg.area.bbox}  CRS={cfg.processing_crs}")
    for stage in STAGE_ORDER:
        path = artefact_path(cfg, stage)
        mark = "✔" if path.exists() else "·"
        typer.echo(f"  {mark} {stage.value:<8} {path}")


@app.command()
def stage(
    name: Annotated[Stage, typer.Argument(help="Fase a executar.")],
    config: ConfigOption = DEFAULT_CONFIG,
) -> None:
    """Executa uma única fase do pipeline."""
    _run(_load(config), name)


@app.command()
def run(
    config: ConfigOption = DEFAULT_CONFIG,
    from_stage: Annotated[Stage, typer.Option("--from", help="Fase inicial.")] = Stage.FETCH,
    to_stage: Annotated[Stage, typer.Option("--to", help="Fase final.")] = Stage.QA,
) -> None:
    """Executa um intervalo de fases (por omissão, até QA — o export é explícito)."""
    cfg = _load(config)
    start, end = STAGE_ORDER.index(from_stage), STAGE_ORDER.index(to_stage)
    for s in STAGE_ORDER[start : end + 1]:
        _run(cfg, s)


if __name__ == "__main__":
    app()
