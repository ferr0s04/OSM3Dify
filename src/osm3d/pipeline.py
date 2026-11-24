"""Fases do pipeline e respetivos artefactos.

Cada fase lê o artefacto da anterior e escreve o seu em
``data/processed/<área>/<NN>_<fase>.*``, para poder ser re-executada isoladamente
(p.ex. voltar a ajustar telhados sem voltar a ler o LiDAR).
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import threading
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from osm3d.config import Config
from osm3d.export.s3db_tags import TagPolicy

if TYPE_CHECKING:
    from shapely.geometry.base import BaseGeometry

    from osm3d.domain.models import Building
    from osm3d.export.osm_edits import ManualRing, OsmEdits
    from osm3d.export.osm_xml import ExportRecord
    from osm3d.geometry_edits import GeometryEdits
    from osm3d.reconstruction.walls import WallProposal
    from osm3d.validation.topology import Adjustment, TopologyResult


class Stage(StrEnum):
    FETCH = "fetch"
    PREPARE = "prepare"
    HEIGHTS = "heights"
    PARTS = "parts"
    ROOFS = "roofs"
    COLOURS = "colours"
    QA = "qa"
    CANDIDATES = "candidates"  # edifícios da cartografia municipal em falta no OSM; opcional
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
        roof_colour=config.export.roof_colour,
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


def _previous(config: Config, stage: Stage) -> Stage:
    """Fase anterior com artefacto próprio (salta as fases de passagem)."""
    idx = STAGE_ORDER.index(stage) - 1
    while _is_passthrough(config, STAGE_ORDER[idx]):
        idx -= 1
    return STAGE_ORDER[idx]


def _input_of(config: Config, stage: Stage) -> Path:
    """Artefacto de entrada: o da fase anterior, saltando fases de passagem."""
    prev = _previous(config, stage)
    return _require(artefact_path(config, prev), prev)


StageState = Literal["done", "missing", "stale"]
# Fases que leem o LAZ/MDT: ficam desatualizadas se os ficheiros raw forem mais recentes.
_READS_RAW = {Stage.ROOFS, Stage.COLOURS, Stage.CANDIDATES}


def _raw_tiles(config: Config) -> tuple[dict[str, Path], dict[str, Path]]:
    return (
        area_tiles(config, config.sources.lidar_dir, (".laz", ".las")),
        area_tiles(config, config.sources.dtm_dir, (".tif", ".tiff")),
    )


def stage_state(config: Config, stage: Stage) -> tuple[StageState, str]:
    """Estado de uma fase da área: feita e atual, em falta ou desatualizada (+ motivo).

    Não deteta alterações de parâmetros no ``config.yaml``: nesse caso refaz-se a pedido.
    """
    if _is_passthrough(config, stage):
        return "done", "sem processamento próprio"
    out = artefact_path(config, stage)
    if not out.exists():
        return "missing", "ainda não processada"
    made = out.stat().st_mtime
    if stage is Stage.FETCH:
        if not cache_matches(out, config.area.tiles):
            return "stale", "obtida para outras quadrículas"
        return "done", ""
    if stage is Stage.CANDIDATES:
        lidar, dtm = _raw_tiles(config)
        if not cache_matches(context_path(config), sorted(set(lidar) & set(dtm))):
            return "stale", "as quadrículas com LAZ e MDT mudaram"
        used = candidates_sources(config) or {}
        if used.get("version") != CANDIDATES_VERSION:
            return "stale", "candidatos de uma versão anterior (com deteção só pelo LiDAR)"
        mpath = municipal_path(config)
        if not mpath.exists() or mpath.stat().st_mtime > made:
            return "stale", "cartografia municipal por obter ou mais recente"
        full = config.sources.municipal.full_path
        if full.exists() and full.stat().st_mtime > made:
            return "stale", "cartografia municipal do concelho descarregada de novo"
    else:
        prev = _previous(config, stage)
        before = artefact_path(config, prev)
        if not before.exists():
            return "stale", f"falta a fase '{prev.value}'"
        if before.stat().st_mtime > made:
            return "stale", f"a fase '{prev.value}' é mais recente"
    if stage in _READS_RAW:
        lidar, dtm = _raw_tiles(config)
        newest = max((p.stat().st_mtime for p in [*lidar.values(), *dtm.values()]), default=0.0)
        if newest > made:
            return "stale", "há LAZ/MDT mais recentes"
    return "done", ""


def run_stage(
    config: Config,
    stage: Stage,
    *,
    force: bool = False,
    progress: Callable[[str], None] | None = None,
) -> Path:
    """Executa uma fase e devolve o caminho do artefacto produzido."""
    if not config.area.tiles and stage is not Stage.EXPORT:
        raise ValueError(
            f"A área {config.area.name} não tem quadrículas: escolhê-las no separador Dados "
            "da aplicação ou com `osm3d area --tiles`"
        )
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
                config.area.bbox,
                config.sources.overpass_url,
                out,
                force=force or stale,
                progress=progress,
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
            orthos = ortho_files(config)
            if progress and not orthos:
                progress("  sem ortofotos: cor só do LiDAR (descarregar ortofotos melhora-a)")
            coloured = colour_area(buildings, lidar, progress, orthos)
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
            walls = accepted_walls(config, reviews)
            path, n_selected, records, topo = export_area(config, buildings, progress, walls)
            if progress:
                n_conf = sum(bool(r.conflicts) for r in records)
                n_rev = sum(b.review_status != "pending" for b in buildings)
                progress(
                    f"  {n_selected} de {len(buildings)} edifícios exportados ({n_rev} revistos), "
                    f"{n_conf} com conflitos de tags (mantido o valor OSM)"
                )
                if topo is not None:
                    progress(f"  topologia: {topo.summary()} (ver o relatório CSV)")
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


def ortho_files(config: Config) -> list[Path]:
    """Ortofotos completas na pasta (as quadrículas do ORTOS não são as do LiDAR)."""
    from osm3d.acquisition.inventory import PARTIAL_SUFFIX, SUFFIXES

    d = config.sources.orthophoto_dir
    if not d.is_dir():
        return []
    return sorted(
        p
        for p in d.iterdir()
        if p.is_file()
        and p.suffix.lower() in SUFFIXES["orthophoto"]
        and not p.name.endswith(PARTIAL_SUFFIX)
    )


def review_buildings(config: Config) -> list[Building]:
    """Edifícios a rever: os do OSM (fase QA) e os em falta no OSM (fase candidates)."""
    from osm3d.export.geopackage import read_buildings

    qa = _require(artefact_path(config, Stage.QA), Stage.QA)
    buildings = read_buildings(qa)
    candidates = artefact_path(config, Stage.CANDIDATES)
    if candidates.exists():
        buildings += [b for b in read_buildings(candidates) if is_municipal(b)]
    return buildings


def is_municipal(b: Building) -> bool:
    """Edifício novo com contorno da cartografia municipal. Artefactos antigos ainda podem
    ter candidatos só do LiDAR: não entram na revisão nem na exportação."""
    return "municipal_gid" in b.diagnostics


def context_path(config: Config) -> Path:
    return area_dir(config) / "osm_context.json"


def geometry_edits(config: Config) -> GeometryEdits:
    """Edições de geometria feitas no mapa da revisão (landuse do OSM e muros)."""
    from osm3d.review import ReviewStore

    return ReviewStore(review_store_path(config)).geometry_edits()


# A aplicação serve pedidos em paralelo (threads): estes ficheiros são escritos por uma
# thread de cada vez. No Windows, substituir um ficheiro que outra thread está a ler falha.
_CONTEXT_LOCK = threading.Lock()
_WALLS_LOCK = threading.Lock()


def effective_context_path(config: Config, edits: GeometryEdits | None = None) -> Path:
    """Contexto OSM com as edições manuais de landuse (o próprio, se não as há).

    Cada versão (contexto + edições) tem o seu ficheiro, que nunca é reescrito: um pedido
    que o está a ler não colide com outro que gera a versão seguinte.
    """
    path = context_path(config)
    edits = geometry_edits(config) if edits is None else edits
    if edits.landuse_empty or not path.exists():
        return path
    from osm3d.geometry_edits import apply_to_context

    sig = f"{path.stat().st_mtime_ns}:{edits.signature()}"
    out = path.with_name(f"osm_context.edited.{hashlib.sha1(sig.encode()).hexdigest()[:12]}.json")
    if out.exists():
        return out
    with _CONTEXT_LOCK:
        if out.exists():
            return out
        data = json.loads(path.read_text(encoding="utf-8"))
        tmp = out.with_name(out.name + ".part")
        tmp.write_text(json.dumps(apply_to_context(data, edits)), encoding="utf-8")
        tmp.replace(out)
        # Versões anteriores (as que ainda estão a ser lidas ficam para a próxima vez).
        for old in path.parent.glob("osm_context.edited*"):
            if old != out:
                with contextlib.suppress(OSError):
                    old.unlink()
    return out


def barriers_path(config: Config) -> Path:
    """Cache dos muros e vedações municipais da área (propostas de muros)."""
    return area_dir(config) / "municipal_barriers.geojson"


def fetch_area_barriers(
    config: Config, *, force: bool = False, progress: Callable[[str], None] | None = None
) -> Path:
    """Muros e vedações municipais das quadrículas da área (+30 m), em cache.

    Com a cópia de todo o concelho descarregada, recortam-se dela (sem pedidos ao
    geoportal); senão, pedem-se ao geoportal só os da área.
    """
    import shapely

    from osm3d.acquisition.municipal import (
        crop_barriers,
        fetch_municipal_barriers,
        full_barriers_usable,
    )
    from osm3d.geospatial.tiling import TileKey

    tiles = config.area.tiles
    if not tiles:
        raise FileNotFoundError("A área não tem quadrículas")
    extent = shapely.union_all([shapely.box(*TileKey.from_dgt_name(t).bounds) for t in tiles])
    bbox = extent.buffer(TILE_CONTEXT_MARGIN_M).bounds
    out = barriers_path(config)
    stale = force or not out.exists() or not cache_matches(out, tiles)
    mcfg = config.sources.municipal
    if full_barriers_usable(mcfg):
        full = mcfg.full_barriers_path
        if stale or full.stat().st_mtime > out.stat().st_mtime:
            crop_barriers(full, bbox, out, progress)
        path = out
    else:
        path = fetch_municipal_barriers(bbox, mcfg, out, force=stale, progress=progress)
    mark_cache(path, tiles)
    return path


def municipal_path(config: Config) -> Path:
    """Cache da cartografia municipal de edifícios (fase candidates)."""
    return area_dir(config) / "municipal_buildings.geojson"


# Margem à volta das quadrículas: o candidato da borda precisa dos vizinhos.
TILE_CONTEXT_MARGIN_M = 30.0


def refresh_context(
    config: Config,
    tiles: list[str] | None = None,
    *,
    force: bool = False,
    progress: Callable[[str], None] | None = None,
) -> Path:
    """Contexto OSM das quadrículas (+30 m): edifícios, landuse, vias, muros (em cache).

    ``tiles``: as quadrículas com LAZ e MDT (as da fase candidates); por omissão, as da
    área. Volta a pedir ao Overpass se a cache é de outras quadrículas, de uma versão
    antiga ou com ``force``.
    """
    import shapely
    from pyproj import Transformer

    from osm3d.acquisition.osm import context_is_current, fetch_context
    from osm3d.geospatial.tiling import TileKey

    if tiles is None:
        lidar, dtm = _raw_tiles(config)
        tiles = sorted(set(lidar) & set(dtm)) or sorted(config.area.tiles)
    if not tiles:
        raise FileNotFoundError("A área não tem quadrículas")
    extent = shapely.union_all([shapely.box(*TileKey.from_dgt_name(t).bounds) for t in tiles])
    to_wgs = Transformer.from_crs(config.processing_crs, "EPSG:4326", always_xy=True)
    x0, y0, x1, y1 = extent.buffer(TILE_CONTEXT_MARGIN_M).bounds
    lons, lats = to_wgs.transform([x0, x1, x0, x1], [y0, y0, y1, y1])
    bbox = (min(lons), min(lats), max(lons), max(lats))
    path = context_path(config)
    stale = not cache_matches(path, tiles) or not context_is_current(path)
    ctx = fetch_context(
        bbox, config.sources.overpass_url, path, force=force or stale, progress=progress
    )
    mark_cache(ctx, tiles)
    return ctx


# Versão dos candidatos: outra = refazer (a 2 deixou de detetar edifícios só pelo LiDAR).
CANDIDATES_VERSION = 2


def _run_candidates(
    config: Config, out: Path, force: bool, progress: Callable[[str], None] | None
) -> Path:
    """Edifícios da cartografia municipal em falta no OSM e confirmados pelo LiDAR, com o
    telhado (LiDAR) e a cor ajustados como aos restantes."""
    import shapely

    from osm3d.acquisition.municipal import (
        crop_municipal,
        fetch_municipal_buildings,
        load_municipal_buildings,
    )
    from osm3d.acquisition.osm import context_timestamp
    from osm3d.export.geopackage import write_buildings
    from osm3d.geospatial.tiling import TileKey
    from osm3d.reconstruction.building import reconstruct_area
    from osm3d.reconstruction.candidates import detect_candidates, load_context
    from osm3d.validation.quality import check_building

    mcfg = config.sources.municipal
    if not mcfg.configured:
        raise ValueError(
            "Os edifícios em falta no OSM vêm da cartografia municipal: configurar o geoportal "
            "em sources.municipal (config/local.yaml)"
        )
    lidar = area_tiles(config, config.sources.lidar_dir, (".laz", ".las"))
    dtm = area_tiles(config, config.sources.dtm_dir, (".tif", ".tiff"))
    tiles = sorted(set(lidar) & set(dtm))
    if not tiles:
        raise FileNotFoundError("Nenhuma quadrícula da área tem LAZ e MDT (descarregar primeiro)")
    # Edifícios OSM das quadrículas (+30 m): um edifício já mapeado logo do outro lado do
    # limite da área não é "em falta".
    extent = shapely.union_all([shapely.box(*TileKey.from_dgt_name(t).bounds) for t in tiles])
    ctx = refresh_context(config, tiles, force=force, progress=progress)
    osm, _ = load_context(ctx, config.processing_crs, None)
    # Os edifícios da fase fetch também contam: um servidor Overpass alternativo pode
    # estar desatualizado, e um edifício já mapeado não pode voltar como "em falta".
    fetched = artefact_path(config, Stage.FETCH)
    if fetched.exists():
        from osm3d.geospatial.footprints import load_footprints

        osm += [b.footprint for b in load_footprints(fetched, config.processing_crs, 0.0)]
    if progress:
        progress(
            f"  contexto OSM ({context_timestamp(ctx)}): {len(osm)} edifícios em "
            f"{len(tiles)} quadrículas"
        )
    mpath = municipal_path(config)
    mx0, my0, mx1, my1 = extent.buffer(TILE_CONTEXT_MARGIN_M).bounds
    stale = force or not mpath.exists() or not cache_matches(mpath, tiles)
    if mcfg.full_path.exists():
        # Cópia de todo o concelho: recorta-se a área localmente, sem pedidos.
        if stale or mcfg.full_path.stat().st_mtime > mpath.stat().st_mtime:
            crop_municipal(mcfg.full_path, (mx0, my0, mx1, my1), mpath, mcfg, progress)
        elif progress:
            progress("  cartografia municipal: recorte da cópia do concelho em cache")
    else:
        fetch_municipal_buildings((mx0, my0, mx1, my1), mcfg, mpath, force=stale, progress=progress)
    mark_cache(mpath, tiles)
    municipal = load_municipal_buildings(mpath)
    if mcfg.barriers:
        # Muros e vedações para as propostas de muros (falhar aqui não trava os edifícios).
        try:
            fetch_area_barriers(config, force=force, progress=progress)
        except Exception as exc:
            if progress:
                progress(f"  muros municipais por obter ({exc}): obter na aplicação")
    # Limite = as próprias quadrículas da área (centróide dentro delas, como no prepare).
    found = detect_candidates(
        lidar,
        dtm,
        osm,
        config.area.polygon(),
        municipal,
        mcfg,
        progress=progress,
        workers=config.workers,
    )
    if progress:
        progress(f"  {len(found)} edifícios da cartografia municipal em falta no OSM")
    # O LiDAR dá a forma e a altura do telhado, como aos edifícios do OSM.
    fitted, _ = reconstruct_area(found, lidar, dtm, config, progress)
    # Cor do telhado, como na fase colours dos edifícios OSM.
    from osm3d.appearance.area import colour_area

    fitted = colour_area(fitted, lidar, progress, ortho_files(config))
    checked = []
    for b in fitted:
        known = {f.code for f in b.qa_flags}
        extra = [f for f in check_building(b) if f.code not in known]
        checked.append(b.model_copy(update={"qa_flags": [*b.qa_flags, *extra]}))
    path = write_buildings(checked, out, config.processing_crs)
    _sources_meta(out).write_text(
        json.dumps({"municipal": True, "version": CANDIDATES_VERSION}), encoding="utf-8"
    )
    return path


def _sources_meta(out: Path) -> Path:
    """Fontes usadas na última corrida dos candidatos (p.ex. a cartografia municipal)."""
    return out.with_name(out.name + ".sources.json")


def candidates_sources(config: Config) -> dict[str, Any] | None:
    """Fontes usadas nos candidatos da área (``None``: sem registo)."""
    meta = _sources_meta(artefact_path(config, Stage.CANDIDATES))
    if not meta.exists():
        return None
    try:
        data = json.loads(meta.read_text(encoding="utf-8"))
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def municipal_status(config: Config) -> dict[str, Any]:
    """Cartografia municipal da área ativa, para a aplicação."""
    from osm3d.acquisition.municipal import read_fetch_meta

    m = config.sources.municipal
    path = municipal_path(config)
    meta: dict[str, Any] = dict(read_fetch_meta(path))
    if path.exists() and meta.get("n") is None:  # caches antigas: contar no ficheiro
        with contextlib.suppress(ValueError, KeyError):
            meta["n"] = len(json.loads(path.read_text(encoding="utf-8"))["features"])
    used = candidates_sources(config)
    return {
        "name": m.name,
        # Há um geoportal configurado (config/local.yaml)? Sem ele, a opção não aparece.
        "configured": m.configured,
        "enabled": m.enabled,
        "viewer_url": m.viewer_url,
        "min_area_m2": m.min_area_m2,
        "cached": path.exists(),
        "n": meta.get("n"),
        "fetched_at": meta.get("fetched_at"),
        "used_in_candidates": None if used is None else used.get("version") == CANDIDATES_VERSION,
        # O recorte da área veio da cópia do concelho (sem pedidos ao geoportal)?
        "from_full": bool(meta.get("from_full")),
        "full": municipal_full_status(config),
    }


def municipal_full_status(config: Config) -> dict[str, Any]:
    """Cópia de todo o concelho (descarregada a pedido, partilhada por todas as áreas):
    edifícios, muros e um download interrompido que se pode retomar."""
    from osm3d.acquisition.municipal import (
        BoxLog,
        barriers_meta,
        full_barriers_usable,
        progress_path,
        read_fetch_meta,
    )

    m = config.sources.municipal
    full, walls = m.full_path, m.full_barriers_path
    meta = read_fetch_meta(full)
    wmeta = barriers_meta(walls) if walls.exists() else {}
    parts = [BoxLog.status(progress_path(p)) for p in (full, walls)]
    partial = [s for s in parts if s]
    return {
        "cached": full.exists(),
        "path": str(full.resolve()),
        "n": meta.get("n"),
        "fetched_at": meta.get("fetched_at"),
        "bytes": (full.stat().st_size if full.exists() else 0)
        + (walls.stat().st_size if walls.exists() else 0),
        "barriers": {
            "configured": bool(m.barriers),
            "cached": full_barriers_usable(m),
            "n": wmeta.get("n"),
            "fetched_at": wmeta.get("fetched_at"),
        },
        # Download interrompido (servidor reiniciado, cancelado, erro): retoma-se.
        "partial": {
            "done": sum(s["done"] for s in partial),
            "total": sum(s["total"] for s in partial),
        }
        if partial
        else None,
    }


def clear_municipal_cache(config: Config) -> None:
    """Apaga a cache da cartografia municipal (é obtida de novo na próxima corrida)."""
    path = municipal_path(config)
    for p in (path, _cache_meta(path), path.with_name(path.name + ".fetch.json")):
        p.unlink(missing_ok=True)  # com a cópia do concelho, volta a ser recortada dela


def review_store_path(config: Config) -> Path:
    """Revisões humanas: fora dos artefactos das fases, para sobreviverem a novas corridas."""
    return area_dir(config) / "reviews.sqlite"


# ------------------------------------------------------------------- muros

WALLS_VERSION = 3  # 2: retaining_wall passa a wall; 3: sem vedações


def walls_cache_path(config: Config) -> Path:
    return area_dir(config) / "wall_proposals.json"


def wall_proposals(config: Config) -> list[WallProposal]:
    """Propostas de muros da área (em cache enquanto os muros municipais e o contexto OSM
    não mudam). Sem muros municipais obtidos: lista vazia."""
    with _WALLS_LOCK:
        return _wall_proposals(config)


def _wall_proposals(config: Config) -> list[WallProposal]:
    from osm3d.acquisition.municipal import load_municipal_barriers
    from osm3d.reconstruction.walls import (
        WallContext,
        WallProposal,
        load_wall_context,
        propose_walls,
    )

    bpath, ctx = barriers_path(config), context_path(config)
    if not bpath.exists():
        return []
    sig = {
        "version": WALLS_VERSION,
        "barriers": bpath.stat().st_mtime,
        "context": ctx.stat().st_mtime if ctx.exists() else None,
    }
    cache = walls_cache_path(config)
    if cache.exists():
        with contextlib.suppress(ValueError, KeyError):
            data = json.loads(cache.read_text(encoding="utf-8"))
            if data["signature"] == sig:
                return [WallProposal.from_json(d) for d in data["walls"]]
    wctx = load_wall_context(ctx, config.processing_crs) if ctx.exists() else WallContext()
    walls = propose_walls(load_municipal_barriers(bpath), wctx)
    tmp = cache.with_suffix(".json.part")
    tmp.write_text(
        json.dumps({"signature": sig, "walls": [w.to_json() for w in walls]}), encoding="utf-8"
    )
    tmp.replace(cache)
    return walls


def effective_walls(config: Config, edits: GeometryEdits | None = None) -> list[WallProposal]:
    """Propostas de muros com as edições do mapa: a geometria editada de cada muro e as
    ligações ao landuse refeitas nos muros editados e nos que ficam perto de um landuse
    editado (contra o landuse já editado)."""
    import dataclasses

    import shapely
    from pyproj import Transformer
    from shapely.geometry import LineString

    from osm3d.geometry_edits import ContextIndex, edited_landuse
    from osm3d.reconstruction.walls import SNAP_M, landuse_runs, load_wall_context

    base = wall_proposals(config)
    edits = geometry_edits(config) if edits is None else edits
    if not edits.walls and edits.landuse_empty:
        return base
    path = effective_context_path(config, edits)
    wctx = load_wall_context(path, config.processing_crs) if path.exists() else None
    near_edited: BaseGeometry | None = None
    if wctx is not None and not edits.landuse_empty:
        raw = json.loads(context_path(config).read_text(encoding="utf-8"))
        changed = set(edited_landuse(ContextIndex.of(raw), edits))
        rings = [r.polygon.exterior for r in wctx.landuse if r.way_id in changed]
        near_edited = shapely.union_all(rings).buffer(SNAP_M + 1.0) if rings else None
    to_crs = Transformer.from_crs("EPSG:4326", config.processing_crs, always_xy=True)
    tree = shapely.STRtree([r.polygon.exterior for r in wctx.landuse]) if wctx else None
    out = []
    for w in base:
        coords = edits.walls.get(w.key)
        geom = w.geometry
        if coords:
            xs, ys = to_crs.transform([c[0] for c in coords], [c[1] for c in coords])
            geom = LineString(list(zip(xs, ys, strict=True)))
        redo = coords is not None or (near_edited is not None and geom.intersects(near_edited))
        if redo and wctx is not None and tree is not None:
            runs = landuse_runs(geom, wctx.landuse, tree) if wctx.landuse else []
            w = dataclasses.replace(w, geometry=geom, runs=runs)
        elif coords is not None:
            w = dataclasses.replace(w, geometry=geom, runs=[])
        out.append(w)
    return out


def _wall_adjustments(config: Config, walls: list[WallProposal]) -> list[Adjustment]:
    """Ajustes dos landuse para o limite passar exatamente pelos muros aceites."""
    from osm3d.reconstruction.walls import SNAP_M, load_wall_context, run_faces
    from osm3d.validation.topology import Adjustment, Feature

    ctx = effective_context_path(config)
    if not ctx.exists() or not any(w.runs for w in walls):
        return []
    rings = {r.way_id: r for r in load_wall_context(ctx, config.processing_crs).landuse}
    out: list[Adjustment] = []
    for w in walls:
        for run in w.runs:
            ring = rings.get(run.way_id)
            if ring is None:
                continue
            feature = Feature(ring.polygon, ring.label, "way", ring.way_id)
            for face, include in run_faces(w, ring.polygon, run):
                out.append(
                    Adjustment(
                        w.key,
                        feature,
                        face,
                        include,
                        f"{ring.label} passa a seguir o muro ({run.end_m - run.start_m:.0f} m, "
                        f"até {run.max_offset_m:.1f} m)",
                        max_width_m=SNAP_M + 0.5,
                    )
                )
    return out


@dataclass
class ExportTopology:
    """Verificação da topologia dos edifícios novos e dos muros na exportação."""

    result: TopologyResult | None = None
    ways_adjusted: int = 0  # landuse/edifícios do OSM ajustados no ficheiro
    not_adjusted: int = 0  # edifícios/muros cujos ajustes ao OSM não se fizeram
    walls: int = 0  # muros novos exportados
    walls_connected: int = 0  # ... ligados ao landuse (limite ajustado ao muro)
    error: str | None = None  # p.ex. Overpass sem resposta: nada ajustado
    context_date: str = "?"

    def summary(self) -> str:
        parts = []
        r = self.result
        if r is not None:
            parts.append(
                f"{r.checked} edifícios novos verificados: {r.trimmed} recortados, "
                f"{r.conflicts} com conflitos"
            )
        if self.walls:
            parts.append(f"{self.walls} muros novos ({self.walls_connected} ligados ao landuse)")
        parts.append(f"{self.ways_adjusted} elementos do OSM ajustados")
        if self.not_adjusted:
            parts.append(f"{self.not_adjusted} edifícios/muros sem o ajuste ao OSM")
        return "; ".join(parts) + (f" — {self.error}" if self.error else "")


ADJUST_FAILED = "osm_adjust_failed"


def export_topology(
    config: Config,
    selected: list[tuple[Building, dict[str, str]]],
    records: list[ExportRecord],
    progress: Callable[[str], None] | None = None,
    walls: list[WallProposal] | None = None,
) -> tuple[list[tuple[Building, dict[str, str]]], ExportTopology | None, OsmEdits | None]:
    """Verifica os edifícios novos a exportar contra o OSM e entre si, e liga os muros.

    Só agora se sabe quais vão juntos (os aceites) e o modo escolhido para cada um. Os
    recortes saem nos contornos; os ajustes a ``landuse``/edifícios do OSM (modo ``osm``
    e muros ao longo de limites) são feitos sobre os dados atuais, pedidos ao Overpass.
    O que se fez e os conflitos que ficam vão para o relatório.
    """
    from osm3d.export.osm_edits import fetch_for_edit, plan_edits
    from osm3d.export.osm_xml import ExportRecord, has_parts
    from osm3d.reconstruction.walls import with_run_vertices
    from osm3d.validation.topology import (
        CONFLICT_CODES,
        TRIMMED,
        TopologyRules,
        load_surroundings,
        resolve_topology,
    )

    walls = walls or []
    ctx = effective_context_path(config)
    has_new = any(b.osm_type == "new" for b, _ in selected) and ctx.exists()
    manual = manual_landuse(config)
    if not has_new and not walls and not manual:
        return selected, None, None
    rules = TopologyRules.from_config(config.topology)
    info = ExportTopology()
    fixed = [b for b, _ in selected]
    adjustments: list[Adjustment] = []
    if has_new:
        env = load_surroundings(ctx, config.processing_crs, artefact_path(config, Stage.FETCH))
        fixed, info.result = resolve_topology(fixed, env, rules)
        info.context_date = env.timestamp
        adjustments += info.result.adjustments
    adjustments += _wall_adjustments(config, walls)
    lines = [(w.key, with_run_vertices(w), w.tags) for w in walls]
    ways = [b for b in fixed if b.osm_type == "way"]
    plan = {
        "skip_ways": {b.osm_id for b in ways if has_parts(b)},
        "versions": {b.osm_id: b.osm_version for b in ways},
        "lines": lines,
    }
    edits: OsmEdits | None = None
    if adjustments or manual:
        way_ids = [a.feature.osm_id for a in adjustments] + list(manual)
        if progress:
            progress(f"  a obter do Overpass as {len(set(way_ids))} ways do OSM a ajustar")
        try:
            xml = fetch_for_edit(way_ids, config.sources.overpass_url, progress)
        except Exception as exc:  # rede/Overpass: exporta-se na mesma, sem ajustes
            first = (str(exc).splitlines() or [type(exc).__name__])[0]
            info.error = f"sem ajustes ao OSM (Overpass: {first})"
        else:
            # Fica junto ao .osm, para se saber sobre que dados se ajustou.
            source = artefact_path(config, Stage.EXPORT).with_name(
                f"{config.area.name}_osm_ajustado_origem.osm"
            )
            source.parent.mkdir(parents=True, exist_ok=True)
            source.write_bytes(xml)
            edits = plan_edits(
                adjustments,
                xml,
                config.processing_crs,
                rules,
                manual=manual,
                **plan,  # type: ignore[arg-type]
            )
    if edits is None and lines:
        # Sem ajustes (ou sem Overpass): os muros vão na mesma, sem ligação ao landuse.
        edits = plan_edits([], b"<osm version='0.6'/>", config.processing_crs, rules, lines=lines)
    if edits is not None:
        info.ways_adjusted = len(edits.ways)
    by_key = {r.key: r for r in records}
    out: list[tuple[Building, dict[str, str]]] = []
    for b, (before, tags) in zip(fixed, selected, strict=True):
        if b.osm_type == "new" and info.result is not None:
            b = _mark_adjust_failures(b, adjustments, edits, info)
            r = by_key.get(b.key)
            if r is not None:
                notes = []
                if b.footprint is not before.footprint:
                    notes += [f.message for f in b.qa_flags if f.code == TRIMMED]
                if edits is not None and b.key in edits.applied:
                    notes.append("OSM ajustado: " + "; ".join(edits.applied[b.key]))
                notes += [
                    f.message
                    for f in b.qa_flags
                    if f.code in CONFLICT_CODES or f.code == ADJUST_FAILED
                ]
                r.topology = " | ".join(notes)
        out.append((b, tags))
    for w in walls:
        info.walls += 1
        applied = edits.applied.get(w.key, []) if edits is not None else []
        failed = edits.failed.get(w.key, []) if edits is not None else []
        if w.runs and (failed or (edits is None or not applied)):
            info.not_adjusted += 1
            failed = failed or [info.error or "ajuste ao landuse por fazer"]
        info.walls_connected += bool(applied) and not failed
        notes = [f"{w.tags.get('barrier')} ({w.layer}, {w.geometry.length:.0f} m)"]
        notes += [f"landuse ajustado: {a}" for a in applied]
        notes += [f"por fazer: {f}" for f in failed]
        records.append(ExportRecord(w.key, True, "muro novo", topology="; ".join(notes)))
    if edits is not None:
        for key, why in sorted(edits.failed.items()):
            if key.startswith("way/") and int(key[4:]) in manual:
                info.not_adjusted += 1
                records.append(
                    ExportRecord(key, False, "edição manual não aplicada", topology="; ".join(why))
                )
        for wid, notes in sorted(edits.way_notes.items()):
            records.append(
                ExportRecord(
                    f"way/{wid}",
                    True,
                    "geometria ajustada a edifícios/muros novos",
                    topology="; ".join(notes),
                )
            )
    return out, info, edits


def manual_landuse(config: Config) -> dict[int, ManualRing]:
    """Landuse editados à mão na revisão, para a exportação: (nós no OSM quando se editou,
    anel novo com (id do nó ou None, x, y) no CRS de processamento)."""
    from pyproj import Transformer

    from osm3d.geometry_edits import ContextIndex, edited_landuse, landuse_ring

    edits = geometry_edits(config)
    path = context_path(config)
    if edits.landuse_empty or not path.exists():
        return {}
    ctx = ContextIndex.of(json.loads(path.read_text(encoding="utf-8")))
    to_crs = Transformer.from_crs("EPSG:4326", config.processing_crs, always_xy=True)
    out: dict[int, ManualRing] = {}
    for wid in edited_landuse(ctx, edits):
        ring = landuse_ring(ctx, edits, wid)
        xs, ys = to_crs.transform([n[1] for n in ring], [n[2] for n in ring])
        out[wid] = (
            list(ctx.refs[wid]),
            [(n[0], float(x), float(y)) for n, x, y in zip(ring, xs, ys, strict=True)],
        )
    return out


def _mark_adjust_failures(
    b: Building, adjustments: list[Adjustment], edits: OsmEdits | None, info: ExportTopology
) -> Building:
    """Ajuste ao OSM que não se fez: o edifício sai sobreposto, sinalizado como conflito."""
    from osm3d.domain.models import QAFlag
    from osm3d.validation.topology import ADJUSTS_OSM

    if not any(a.building_key == b.key for a in adjustments):
        return b
    failed = edits.failed.get(b.key) if edits is not None else [info.error or "?"]
    if not failed:
        return b
    info.not_adjusted += 1
    flags = [f for f in b.qa_flags if f.code != ADJUSTS_OSM]
    flags.append(
        QAFlag(
            code=ADJUST_FAILED,
            message="Ajuste ao OSM por fazer: " + "; ".join(failed),
            severity="warning",
        )
    )
    return b.model_copy(update={"qa_flags": flags})


def accepted_walls(config: Config, reviews: dict[str, Any]) -> list[WallProposal]:
    """Muros aceites e não arquivados (as revisões têm a chave ``wall/<id>``), com as
    edições do mapa."""
    out = []
    for w in effective_walls(config):
        r = reviews.get(w.key)
        if r is not None and r.status == "accepted" and not r.archived:
            out.append(w)
    return out


def export_area(
    config: Config,
    buildings: list[Building],
    progress: Callable[[str], None] | None = None,
    walls: list[WallProposal] | None = None,
) -> tuple[Path, int, list[ExportRecord], ExportTopology | None]:
    """Escreve o ``.osm`` para o JOSM e o relatório CSV.

    Devolve (caminho, n.º de edifícios exportados, registos, topologia/muros).
    """
    from osm3d.export.osm_xml import plan_export, write_josm_file, write_report

    out = artefact_path(config, Stage.EXPORT)
    selected, records = plan_export(
        buildings,
        tag_policy(config),
        include_pending=config.export.include_pending,
        overwrite=config.export.overwrite_existing_tags,
    )
    selected, topo, edits = export_topology(config, selected, records, progress, walls)
    write_report(records, out.with_name(f"{config.area.name}_export.csv"))
    source = _require(artefact_path(config, Stage.FETCH), Stage.FETCH)
    write_josm_file(
        selected,
        source,
        out,
        upload=config.export.josm_upload,
        policy=tag_policy(config),
        crs=config.processing_crs,
        edits=edits,
    )
    return out, len(selected), records, topo
