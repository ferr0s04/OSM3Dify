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


def _load(path: Path, *, active_area: bool = True) -> Config:
    """Configuração; com ``active_area``, a área é a ativa (``osm3d area``/aplicação)."""
    from osm3d.areas import with_active_area

    if not path.exists():
        raise typer.BadParameter(f"Configuração não encontrada: {path}")
    cfg = load_config(path)
    return with_active_area(cfg) if active_area else cfg


def _run(config: Config, stage: Stage, force: bool = False) -> None:
    typer.echo(f"[{stage.value}] …")
    try:
        out = run_stage(config, stage, force=force, progress=typer.echo)
    except (NotImplementedError, FileNotFoundError) as exc:
        typer.secho(f"[{stage.value}] {exc}", fg=typer.colors.YELLOW, err=True)
        raise typer.Exit(code=2) from exc
    typer.secho(f"[{stage.value}] → {out}", fg=typer.colors.GREEN)


@app.command()
def review(
    config: ConfigOption = DEFAULT_CONFIG,
    host: Annotated[str, typer.Option(help="Endereço (só local por omissão).")] = "127.0.0.1",
    port: Annotated[int, typer.Option(help="Porta HTTP.")] = 8765,
    browser: Annotated[bool, typer.Option(help="Abrir o browser.")] = True,
) -> None:
    """Aplicação de revisão: mapa, 3D (modelo vs. LiDAR), edição de tags, exportação.

    Inclui o separador "Dados": seleção de quadrículas no mapa, download da DGT em
    segundo plano e eliminação de ficheiros raw.
    """
    import threading
    import webbrowser

    import uvicorn

    from osm3d.app.server import create_app

    # Configuração base (config.yaml): a aplicação gere a área ativa e pode mudá-la.
    application = create_app(_load(config, active_area=False))
    ctx = application.state.ctx
    typer.echo(f"Área ativa: {ctx.cfg.area.name}")
    if ctx.review_error:
        typer.secho(
            f"{ctx.review_error}. Usar o separador Dados para descarregar e processar.",
            fg=typer.colors.YELLOW,
        )
    url = f"http://{host}:{port}/"
    typer.secho(f"Aplicação de revisão em {url} (Ctrl+C para terminar)", fg=typer.colors.GREEN)
    if browser:
        threading.Timer(1.0, webbrowser.open, args=(url,)).start()
    uvicorn.run(application, host=host, port=port, log_level="warning")


@app.command()
def evaluate(config: ConfigOption = DEFAULT_CONFIG) -> None:
    """Compara os resultados com as tags roof:shape / building:levels do OSM."""
    from osm3d.evaluation.benchmark import evaluate as run_eval
    from osm3d.evaluation.benchmark import format_report, write_csv
    from osm3d.export.geopackage import read_buildings

    cfg = _load(config)
    src = next((p for s in (Stage.QA, Stage.ROOFS) if (p := artefact_path(cfg, s)).exists()), None)
    if src is None:
        raise typer.BadParameter("Correr primeiro: osm3d run --to qa")
    report = run_eval(read_buildings(src), cfg.roofs.min_shape_confidence)
    typer.echo(f"Fonte: {src}\n")
    typer.echo(format_report(report))
    csv_path = write_csv(report, cfg.paths.output / f"{cfg.area.name}_avaliacao.csv")
    typer.echo(f"\nDetalhe por edifício: {csv_path}")


@app.command()
def area(
    name: Annotated[
        str | None, typer.Argument(help="Id da área (area-1, …) a ativar ou alterar.")
    ] = None,
    new: Annotated[bool, typer.Option("--new", help="Criar uma área nova (id gerado).")] = False,
    tiles: Annotated[
        str | None, typer.Option(help="Quadrículas DGT separadas por vírgulas (155480,156480).")
    ] = None,
    relation: Annotated[
        int | None,
        typer.Option(help="Em vez de --tiles: as quadrículas que cobrem este limite OSM."),
    ] = None,
    label: Annotated[str | None, typer.Option(help="Nome a mostrar (opcional).")] = None,
    remove: Annotated[bool, typer.Option("--remove", help="Remover a área do registo.")] = False,
    config: ConfigOption = DEFAULT_CONFIG,
) -> None:
    """Lista as áreas; com ID, ativa-a (e altera com --tiles/--relation/--label).

    Uma área é um conjunto de quadrículas de 1 km: o LAZ/MDT e os edifícios OSM
    processados são sempre os dessas quadrículas. ``--new`` cria uma área (area-<n>).
    """
    from osm3d.areas import area_store
    from osm3d.config import AreaConfig

    base = _load(config, active_area=False)
    store = area_store(base)
    if name is None and not new:
        active = store.active().name
        for a in store.areas().values():
            mark = "●" if a.name == active else " "
            done = (
                "✔"
                if artefact_path(base.model_copy(update={"area": a}), Stage.QA).exists()
                else "·"
            )
            typer.echo(f"{mark} {done} {a.name:<24} {len(a.tiles):>4} quadrículas  {a.label or ''}")
        typer.echo(f"\nRegisto: {store.path}  (● ativa, ✔ processada)")
        return
    if remove and name:
        if name == store.active().name:
            raise typer.BadParameter("Não se pode remover a área ativa")
        store.delete(name)
        typer.echo(f"Removida: {name} (os dados em data/processed/{name} ficam)")
        return
    if name and name not in store.areas():
        raise typer.BadParameter(f"Área desconhecida: {name} (criar com --new)")
    if tiles is None and relation is None:
        if new or name is None:
            raise typer.BadParameter("Uma área nova precisa de --tiles ou --relation")
        if label is not None:
            store.save(store.get(name).model_copy(update={"label": label or None}))
        store.activate(name)
        typer.secho(f"Área ativa: {name}", fg=typer.colors.GREEN)
        return
    names: list[str]
    if tiles is not None:
        names = [t.strip() for t in tiles.split(",") if t.strip()]
    else:
        from shapely.geometry import shape

        from osm3d.acquisition.osm import relation_boundary
        from osm3d.geospatial.tiling import tiles_in_geometry

        assert relation is not None
        found, geojson = relation_boundary(relation)
        names = [k.dgt_name for k in tiles_in_geometry(shape(geojson), base.processing_crs)]
        label = label or found
    if new or name is None:
        area_cfg = store.create(names, label, activate=True)
    else:
        previous = store.get(name)
        area_cfg = AreaConfig(name=name, label=label or previous.label, tiles=names)
        store.save(area_cfg, activate=True)
    typer.secho(
        f"Área ativa: {area_cfg.name} ({len(area_cfg.tiles)} quadrículas)", fg=typer.colors.GREEN
    )


@app.command()
def version() -> None:
    """Mostra a versão."""
    typer.echo(__version__)


@app.command()
def info(config: ConfigOption = DEFAULT_CONFIG) -> None:
    """Mostra a configuração ativa e os artefactos de cada fase."""
    cfg = _load(config)
    typer.echo(
        f"Área ativa: {cfg.area.name}  {len(cfg.area.tiles)} quadrículas  CRS={cfg.processing_crs}"
        "  (mudar com `osm3d area` ou na aplicação)"
    )
    typer.echo(f"  quadrículas: {', '.join(cfg.area.tiles)}")
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
    area: Annotated[bool, typer.Option("--area", help="As quadrículas da área ativa.")] = False,
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
    from osm3d.acquisition.dgt import DEFAULT_STAC_URL, DgtClient, DgtError, tile_items
    from osm3d.env import require_env
    from osm3d.geospatial.tiling import TileKey

    cfg = _load(config)
    if area:
        keys = cfg.area.tile_keys
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
            items = tile_items(client, key, coll, cfg.processing_crs)
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
