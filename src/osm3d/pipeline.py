"""Fases do pipeline e respetivos artefactos.

Cada fase lê o artefacto da anterior e escreve o seu em
``data/processed/<área>/<NN>_<fase>.*``, para poder ser re-executada isoladamente
(p.ex. voltar a ajustar telhados sem voltar a ler o LiDAR).
"""

from __future__ import annotations

from collections.abc import Callable
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING

from osm3d.config import Config
from osm3d.export.s3db_tags import TagPolicy

if TYPE_CHECKING:
    from osm3d.domain.models import Building
    from osm3d.export.osm_xml import ExportRecord


class Stage(StrEnum):
    FETCH = "fetch"
    PREPARE = "prepare"
    HEIGHTS = "heights"
    PARTS = "parts"
    ROOFS = "roofs"
    COLOURS = "colours"
    QA = "qa"
    CANDIDATES = "candidates"  # edifícios em falta no OSM (LiDAR); opcional
    EXPORT = "export"


STAGE_ORDER: list[Stage] = list(Stage)

_ARTEFACT_SUFFIX: dict[Stage, str] = {Stage.FETCH: ".osm", Stage.EXPORT: ".osm"}


def area_dir(config: Config) -> Path:
    return config.paths.processed / config.area.name


def artefact_path(config: Config, stage: Stage) -> Path:
    if stage is Stage.EXPORT:
        return config.paths.output / f"{config.area.name}_josm.osm"
    index = STAGE_ORDER.index(stage)
    suffix = _ARTEFACT_SUFFIX.get(stage, ".gpkg")
    return area_dir(config) / f"{index:02d}_{stage.value}{suffix}"


def tag_policy(config: Config) -> TagPolicy:
    return TagPolicy(
        min_shape_confidence=config.roofs.min_shape_confidence,
        min_colour_confidence=config.appearance.min_colour_confidence,
        height_rounding_m=config.export.height_rounding_m,
        roof_height_rounding_m=config.export.roof_height_rounding_m,
    )


# Fases ainda sem processamento próprio: passam o artefacto anterior adiante.
# HEIGHTS: integrada em ROOFS (a altura vem do topo do modelo de telhado).
# PARTS: Fase 4 — por agora, uma parte por edifício.
# COLOURS: só quando appearance.enabled.
_PASSTHROUGH = {Stage.HEIGHTS, Stage.PARTS}


def _is_passthrough(config: Config, stage: Stage) -> bool:
    return stage in _PASSTHROUGH or (stage is Stage.COLOURS and not config.appearance.enabled)


def _require(path: Path, stage: Stage) -> Path:
    if not path.exists():
        raise FileNotFoundError(f"Falta o artefacto {path}: correr primeiro a fase '{stage.value}'")
    return path


def _input_of(config: Config, stage: Stage) -> Path:
    """Artefacto de entrada: o da fase anterior, saltando fases de passagem."""
    idx = STAGE_ORDER.index(stage) - 1
    while _is_passthrough(config, STAGE_ORDER[idx]):
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
    if _is_passthrough(config, stage):
        return _input_of(config, stage)

    match stage:
        case Stage.FETCH:
            from osm3d.acquisition.osm import fetch_buildings

            # Caixa envolvente das quadrículas da área; o recorte exato é feito no prepare.
            stale = not cache_matches(out, config.area.tiles)
            if stale and out.exists() and progress:
                progress("  OSM em cache não é destas quadrículas: a obter de novo")
            path = fetch_buildings(
                config.area.bbox, config.sources.overpass_url, out, force=force or stale
            )
            mark_cache(path, config.area.tiles)
            return path
        case Stage.PREPARE:
            from osm3d.export.geopackage import write_buildings
            from osm3d.geospatial.footprints import load_footprints

            buildings = in_area(
                config,
                load_footprints(
                    _input_of(config, stage), config.processing_crs, config.footprints.min_area_m2
                ),
            )
            if progress:
                progress(f"  {len(buildings)} edifícios nas {len(config.area.tiles)} quadrículas")
            return write_buildings(buildings, out, config.processing_crs)
        case Stage.ROOFS:
            from osm3d.export.geopackage import read_buildings, write_buildings
            from osm3d.reconstruction.building import reconstruct_area

            buildings = read_buildings(_input_of(config, stage))
            lidar = area_tiles(config, config.sources.lidar_dir, (".laz", ".las"))
            dtm = area_tiles(config, config.sources.dtm_dir, (".tif", ".tiff"))
            if progress:
                both = sorted(set(lidar) & set(dtm))
                missing = sorted(set(config.area.tiles) - set(both))
                progress(f"  quadrículas com LAZ+MDT: {len(both)} de {len(config.area.tiles)}")
                if missing:
                    progress(f"  sem LAZ+MDT (edifícios sem reconstrução): {', '.join(missing)}")
            result, _ = reconstruct_area(buildings, lidar, dtm, config, progress)
            return write_buildings(result, out, config.processing_crs)
        case Stage.COLOURS:
            from osm3d.appearance.area import colour_area
            from osm3d.export.geopackage import read_buildings, write_buildings

            buildings = read_buildings(_input_of(config, stage))
            lidar = area_tiles(config, config.sources.lidar_dir, (".laz", ".las"))
            coloured = colour_area(buildings, lidar, progress)
            return write_buildings(coloured, out, config.processing_crs)
        case Stage.QA:
            from osm3d.export.geopackage import read_buildings, write_buildings
            from osm3d.validation.quality import check_building

            checked = []
            for b in read_buildings(_input_of(config, stage)):
                known = {f.code for f in b.qa_flags}
                extra = [f for f in check_building(b) if f.code not in known]
                checked.append(b.model_copy(update={"qa_flags": [*b.qa_flags, *extra]}))
            return write_buildings(checked, out, config.processing_crs)
        case Stage.CANDIDATES:
            return _run_candidates(config, out, force, progress)
        case Stage.EXPORT:
            from osm3d.review import ReviewStore, apply_review

            reviews = ReviewStore(review_store_path(config)).all()
            buildings = [apply_review(b, reviews.get(b.key)) for b in review_buildings(config)]
            path, n_selected, records = export_area(config, buildings)
            if progress:
                n_conf = sum(bool(r.conflicts) for r in records)
                n_rev = sum(b.review_status != "pending" for b in buildings)
                progress(
                    f"  {n_selected} de {len(buildings)} edifícios exportados ({n_rev} revistos), "
                    f"{n_conf} com conflitos de tags (mantido o valor OSM)"
                )
            return path
        case _:
            raise NotImplementedError(f"Fase '{stage.value}' ainda não implementada")


def _cache_meta(path: Path) -> Path:
    return path.with_name(path.name + ".meta.json")


def cache_matches(path: Path, tiles: list[str]) -> bool:
    """O ficheiro em cache (OSM) foi obtido para estas quadrículas?

    Sem registo (cache de uma versão anterior), não se sabe para que quadrículas foi
    obtido: conta como desatualizado.
    """
    import json

    meta = _cache_meta(path)
    if not (path.exists() and meta.exists()):
        return False
    try:
        return bool(json.loads(meta.read_text(encoding="utf-8")).get("tiles") == sorted(tiles))
    except ValueError:
        return False


def mark_cache(path: Path, tiles: list[str]) -> None:
    import json

    _cache_meta(path).write_text(json.dumps({"tiles": sorted(tiles)}), encoding="utf-8")


def in_area(config: Config, buildings: list[Building]) -> list[Building]:
    """Edifícios da área: centróide dentro das quadrículas (os mesmos do LAZ/MDT).

    Cada edifício pertence a uma só quadrícula (a do centróide), como em
    ``summarize_tiles``: áreas vizinhas não partilham edifícios.
    """
    import shapely

    poly = config.area.polygon()
    shapely.prepare(poly)
    return [b for b in buildings if poly.contains(b.footprint.centroid)]


def area_tiles(config: Config, directory: Path, suffixes: tuple[str, ...]) -> dict[str, Path]:
    """Ficheiros locais das quadrículas da área (as outras quadrículas são ignoradas)."""
    from osm3d.geospatial.point_clouds import index_tiles

    wanted = set(config.area.tiles)
    return {t: p for t, p in index_tiles(directory, suffixes).items() if t in wanted}


def review_buildings(config: Config) -> list[Building]:
    """Edifícios a rever: os do OSM (fase QA) e os em falta no OSM (fase candidates)."""
    from osm3d.export.geopackage import read_buildings

    qa = _require(artefact_path(config, Stage.QA), Stage.QA)
    buildings = read_buildings(qa)
    candidates = artefact_path(config, Stage.CANDIDATES)
    if candidates.exists():
        buildings += read_buildings(candidates)
    return buildings


def context_path(config: Config) -> Path:
    return area_dir(config) / "osm_context.json"


def _run_candidates(
    config: Config, out: Path, force: bool, progress: Callable[[str], None] | None
) -> Path:
    """Deteta edifícios em falta no OSM e ajusta-lhes o telhado como aos restantes."""
    import shapely
    from pyproj import Transformer

    from osm3d.acquisition.osm import fetch_context
    from osm3d.export.geopackage import write_buildings
    from osm3d.geospatial.tiling import TileKey
    from osm3d.reconstruction.building import reconstruct_area
    from osm3d.reconstruction.candidates import detect_candidates, load_context
    from osm3d.validation.quality import check_building

    lidar = area_tiles(config, config.sources.lidar_dir, (".laz", ".las"))
    dtm = area_tiles(config, config.sources.dtm_dir, (".tif", ".tiff"))
    tiles = sorted(set(lidar) & set(dtm))
    if not tiles:
        raise FileNotFoundError("Nenhuma quadrícula da área tem LAZ e MDT (descarregar primeiro)")
    # Edifícios OSM das quadrículas (+30 m): um edifício já mapeado logo do outro lado do
    # limite da área não é "em falta".
    extent = shapely.union_all([shapely.box(*TileKey.from_dgt_name(t).bounds) for t in tiles])
    to_wgs = Transformer.from_crs(config.processing_crs, "EPSG:4326", always_xy=True)
    x0, y0, x1, y1 = extent.buffer(30).bounds
    lons, lats = to_wgs.transform([x0, x1, x0, x1], [y0, y0, y1, y1])
    bbox = (min(lons), min(lats), max(lons), max(lats))
    stale = not cache_matches(context_path(config), tiles)
    ctx = fetch_context(
        bbox, config.sources.overpass_url, context_path(config), force=force or stale
    )
    mark_cache(ctx, tiles)
    osm, _ = load_context(ctx, config.processing_crs, None)
    if progress:
        progress(f"  contexto OSM: {len(osm)} edifícios em {len(tiles)} quadrículas")
    # Limite = as próprias quadrículas da área (centróide dentro delas, como no prepare).
    found = detect_candidates(lidar, dtm, osm, config.area.polygon())
    if progress:
        progress(f"  {len(found)} edifícios em falta no OSM (candidatos)")
    fitted, _ = reconstruct_area(found, lidar, dtm, config, progress)
    checked = []
    for b in fitted:
        known = {f.code for f in b.qa_flags}
        extra = [f for f in check_building(b) if f.code not in known]
        checked.append(b.model_copy(update={"qa_flags": [*b.qa_flags, *extra]}))
    return write_buildings(checked, out, config.processing_crs)


def review_store_path(config: Config) -> Path:
    """Revisões humanas: fora dos artefactos das fases, para sobreviverem a novas corridas."""
    return area_dir(config) / "reviews.sqlite"


def export_area(config: Config, buildings: list[Building]) -> tuple[Path, int, list[ExportRecord]]:
    """Escreve o ``.osm`` para o JOSM e o relatório CSV. Devolve (caminho, n.º, registos)."""
    from osm3d.export.osm_xml import plan_export, write_josm_file, write_report

    out = artefact_path(config, Stage.EXPORT)
    selected, records = plan_export(
        buildings,
        tag_policy(config),
        include_pending=config.export.include_pending,
        overwrite=config.export.overwrite_existing_tags,
    )
    write_report(records, out.with_name(f"{config.area.name}_export.csv"))
    source = _require(artefact_path(config, Stage.FETCH), Stage.FETCH)
    write_josm_file(
        selected,
        source,
        out,
        upload=config.export.josm_upload,
        policy=tag_policy(config),
        crs=config.processing_crs,
    )
    return out, len(selected), records
