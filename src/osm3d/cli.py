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


def _run(config: Config, stage: Stage, force: bool = False) -> None:
    try:
        out = run_stage(config, stage, force=force)
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
    force: Annotated[bool, typer.Option("--force", help="Ignorar cache e refazer.")] = False,
) -> None:
    """Executa uma única fase do pipeline."""
    _run(_load(config), name, force)


@app.command()
def tiles(
    config: ConfigOption = DEFAULT_CONFIG,
    top: Annotated[int, typer.Option(help="Número de quadrículas a mostrar.")] = 10,
) -> None:
    """Resume os edifícios OSM por quadrícula de 1 km (requer a fase fetch)."""
    from osm3d.geospatial.footprints import load_footprints
    from osm3d.geospatial.tiling import summarize_tiles

    cfg = _load(config)
    osm_file = artefact_path(cfg, Stage.FETCH)
    if not osm_file.exists():
        raise typer.BadParameter("Correr primeiro: osm3d stage fetch")
    buildings = load_footprints(osm_file, cfg.processing_crs, cfg.footprints.min_area_m2)
    summaries = summarize_tiles(buildings)
    ranked = sorted(
        summaries, key=lambda s: (s.n_distinct_shapes, s.n_roof_shape, s.n_buildings), reverse=True
    )
    typer.echo(f"{len(buildings)} edifícios em {len(summaries)} quadrículas de 1 km\n")
    typer.echo(
        f"{'DGT':<8}{'x0_y0 (3763)':<16}{'edif.':>6}{'levels':>7}{'roof:shape':>11}"
        f"{'atrav.':>7}  formas / bbox WGS84"
    )
    for s in ranked[:top]:
        shapes = ", ".join(f"{k}={v}" for k, v in s.roof_shapes.most_common())
        w, so, e, n = s.key.bounds_wgs84(cfg.processing_crs)
        typer.echo(
            f"{s.key.dgt_name:<8}{s.key!s:<16}{s.n_buildings:>6}{s.n_levels:>7}"
            f"{s.n_roof_shape:>11}{s.n_crossing:>7}  {shapes or '-'}"
        )
        typer.echo(f"{'':<55}  [{w:.5f}, {so:.5f}, {e:.5f}, {n:.5f}]")


@app.command()
def dgt(
    tile: Annotated[
        list[str] | None, typer.Option("--tile", "-t", help="Identificador DGT (p.ex. 155480).")
    ] = None,
    area: Annotated[
        bool, typer.Option("--area", help="Todas as quadrículas da bbox da área de estudo.")
    ] = False,
    product: Annotated[
        list[str] | None,
        typer.Option("--product", "-p", help="lidar, dtm, dsm, orthophoto (omissão: lidar, dtm)."),
    ] = None,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Só pesquisar e listar o que está disponível.")
    ] = False,
    config: ConfigOption = DEFAULT_CONFIG,
) -> None:
    """Pesquisa e descarrega quadrículas do Centro de Dados da DGT (credenciais em .env)."""
    from osm3d.acquisition.dgt import DEFAULT_STAC_URL, DgtClient, DgtError, latest_versions
    from osm3d.env import require_env
    from osm3d.geospatial.tiling import TileKey, tiles_in_bbox

    cfg = _load(config)
    if area:
        keys = tiles_in_bbox(cfg.area.bbox, cfg.processing_crs)
    elif tile:
        keys = [TileKey.from_dgt_name(t) for t in tile]
    else:
        raise typer.BadParameter("Indicar --tile <id> (repetível) ou --area")
    products = product or ["lidar", "dtm"]
    collections = {p: cfg.sources.dgt_collections[p] for p in products}

    user, password = require_env("DGT_USER", "DGT_PASSWORD")
    client = DgtClient(
        user,
        password,
        cfg.sources.dgt_stac_url or DEFAULT_STAC_URL,
        delay_s=cfg.sources.dgt_delay_s,
    )
    try:
        client.login()
    except DgtError as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(1) from exc
    typer.echo(f"Sessão iniciada. {len(keys)} quadrícula(s), produtos: {', '.join(products)}")

    missing: list[str] = []
    for key in keys:
        for prod, coll in collections.items():
            found = [
                it
                for it in client.search(key.bounds_wgs84(cfg.processing_crs, inset_m=50), [coll])
                if it.tile == key.dgt_name
            ]
            items = latest_versions(found)
            if not items:
                missing.append(f"{key.dgt_name}/{coll}")
                typer.secho(f"{key.dgt_name} {coll}: não disponível", fg=typer.colors.YELLOW)
                continue
            for it in items:
                for asset in it.assets:
                    label = f"{key.dgt_name} {coll}: {it.id} (v{it.version})"
                    if dry_run:
                        typer.echo(f"{label} → {asset.href}")
                        continue
                    typer.echo(f"{label} …")
                    path = client.download(
                        it, asset, cfg.sources.product_dir(prod), progress=typer.echo
                    )
                    size_mb = path.stat().st_size / 1e6
                    typer.secho(f"  ✔ {path} ({size_mb:.0f} MB)", fg=typer.colors.GREEN)
    if missing:
        typer.secho(f"Sem dados: {', '.join(missing)}", fg=typer.colors.YELLOW)


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
