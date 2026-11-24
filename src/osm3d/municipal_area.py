"""Cartografia municipal de uma área: as câmaras que a cobrem, os recortes e a junção.

Uma área pode estar num concelho ou na fronteira entre vários. Para cada câmara
configurada (``sources.municipalities``) cuja extensão toca a área:

1. recorta-se a cópia do concelho, se existe (sem pedidos); senão pede-se só a área à
   fonte (MapGuide/ArcGIS) ou, num ficheiro de dados abertos, descarrega-se o ficheiro;
2. cada recorte fica em cache em ``<área>/municipal/<câmara>.geojson``;
3. juntam-se todos em ``municipal_buildings.geojson`` (o que a fase candidates lê): um
   edifício que duas cartografias têm (na fronteira) fica só uma vez, o da primeira.

Os muros seguem o mesmo caminho (``municipal_barriers.geojson``).

A extensão de cada câmara vem do ``bbox`` do config, da cópia do concelho ou, uma vez, da
própria fonte (fica em cache): escolher as câmaras de uma área não precisa de rede.
"""

from __future__ import annotations

import contextlib
import dataclasses
import json
import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any

import shapely

from osm3d.acquisition.municipal import (
    MunicipalBarrier,
    MunicipalBuilding,
    MunicipalError,
    crop_barriers,
    crop_municipal,
    fetch_full_municipality,
    fetch_municipal_barriers,
    fetch_municipal_buildings,
    full_barriers_usable,
    load_municipal_barriers,
    load_municipal_buildings,
    read_fetch_meta,
    write_barriers,
    write_municipal,
)
from osm3d.config import Config, MunicipalSourceConfig
from osm3d.pipeline import (
    TILE_CONTEXT_MARGIN_M,
    area_dir,
    barriers_path,
    cache_matches,
    mark_cache,
    municipal_path,
)

log = logging.getLogger(__name__)

BBox = tuple[float, float, float, float]
Progress = Callable[[str], None] | None
SAME_BUILDING = 0.5  # fração da área partilhada acima da qual é o mesmo edifício


def area_bbox(config: Config, tiles: list[str] | None = None) -> BBox:
    """Caixa das quadrículas da área (+30 m), EPSG:3763."""
    from osm3d.geospatial.tiling import TileKey

    tiles = tiles if tiles is not None else config.area.tiles
    if not tiles:
        raise FileNotFoundError("A área não tem quadrículas")
    extent = shapely.union_all([shapely.box(*TileKey.from_dgt_name(t).bounds) for t in tiles])
    x0, y0, x1, y1 = extent.buffer(TILE_CONTEXT_MARGIN_M).bounds
    return (x0, y0, x1, y1)


# ------------------------------------------------------------------ câmaras


def _extent_cache(src: MunicipalSourceConfig) -> Path:
    return src.cache_dir / f"{src.key}.extent.json"


def source_extent(src: MunicipalSourceConfig, *, network: bool = True) -> BBox | None:
    """Extensão do concelho (EPSG:3763): config, cópia do concelho, cache ou a fonte."""
    if src.bbox is not None:
        return src.bbox
    meta_bbox = read_fetch_meta(src.full_path).get("bbox")
    if isinstance(meta_bbox, list) and len(meta_bbox) == 4:
        return tuple(float(v) for v in meta_bbox)  # type: ignore[return-value]
    cache = _extent_cache(src)
    with contextlib.suppress(OSError, ValueError, TypeError):
        values = json.loads(cache.read_text(encoding="utf-8"))
        return tuple(float(v) for v in values)  # type: ignore[return-value]
    if not network:
        return None
    from osm3d.acquisition.municipal_providers import FileSource, provider_for

    provider = provider_for(src)
    if isinstance(provider, FileSource) and not provider.path.exists():
        return None  # não se descarrega um ficheiro inteiro só para saber a extensão
    try:
        extent = provider.extent()
    except Exception as exc:  # rede/geoportal: sem extensão, a câmara entra na mesma
        log.warning("Extensão de %s por obter: %s", src.name, exc)
        return None
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps(list(extent)), encoding="utf-8")
    return extent


def sources_for_area(
    config: Config, bbox: BBox | None = None, *, network: bool = True
) -> list[MunicipalSourceConfig]:
    """Câmaras configuradas cuja extensão toca a área (com uma só, essa).

    Uma câmara de extensão desconhecida entra (melhor pedir a mais do que faltar).
    """
    sources = config.sources.municipal_sources
    if len(sources) <= 1:
        return sources
    bbox = bbox or area_bbox(config)
    area = shapely.box(*bbox)
    out = []
    for s in sources:
        e = source_extent(s, network=network)
        if e is None or shapely.box(*e).intersects(area):
            out.append(s)
    return out


# ------------------------------------------------------------------ edifícios


def crop_path(config: Config, src: MunicipalSourceConfig, kind: str = "buildings") -> Path:
    suffix = "" if kind == "buildings" else "-muros"
    return area_dir(config) / "municipal" / f"{src.key}{suffix}.geojson"


def _crop_buildings(
    config: Config,
    src: MunicipalSourceConfig,
    bbox: BBox,
    tiles: list[str],
    force: bool,
    progress: Progress,
) -> list[MunicipalBuilding]:
    path = crop_path(config, src)
    stale = force or not path.exists() or not cache_matches(path, tiles)
    full = src.full_path
    if full.exists():
        # Cópia de todo o concelho: recorta-se a área localmente, sem pedidos.
        if stale or full.stat().st_mtime > path.stat().st_mtime:
            crop_municipal(full, bbox, path, src, progress)
    elif src.kind == "file":
        # Ficheiro de dados abertos: descarrega-se uma vez e fica como cópia do concelho.
        fetch_full_municipality(src, progress=progress)
        crop_municipal(full, bbox, path, src, progress)
    else:
        fetch_municipal_buildings(bbox, src, path, force=stale, progress=progress)
    mark_cache(path, tiles)
    return [
        b if b.source else dataclasses.replace(b, source=src.name)
        for b in load_municipal_buildings(path)
    ]


def merge_buildings(groups: list[list[MunicipalBuilding]]) -> list[MunicipalBuilding]:
    """Junta as cartografias: um edifício que já veio de outra (fronteira) não se repete."""
    out: list[MunicipalBuilding] = []
    for group in groups:
        tree = shapely.STRtree([b.geometry for b in out]) if out else None
        kept = list(out)
        for b in group:
            if tree is not None:
                dup = False
                for i in tree.query(b.geometry):
                    other = kept[i].geometry
                    inter = b.geometry.intersection(other).area
                    if inter > SAME_BUILDING * min(b.geometry.area, other.area):
                        dup = True
                        break
                if dup:
                    continue
            out.append(b)
    return out


def area_buildings(
    config: Config,
    tiles: list[str],
    *,
    force: bool = False,
    progress: Progress = None,
) -> tuple[list[MunicipalBuilding], list[MunicipalSourceConfig]]:
    """Edifícios municipais da área (todas as câmaras que a cobrem) e essas câmaras."""
    bbox = area_bbox(config, tiles)
    sources = sources_for_area(config, bbox)
    if not sources:
        raise MunicipalError(
            "Nenhuma câmara configurada cobre esta área (sources.municipalities em "
            "config/local.yaml)"
        )
    groups = []
    for src in sources:
        if progress and len(sources) > 1:
            progress(f"  {src.name}:")
        groups.append(_crop_buildings(config, src, bbox, tiles, force, progress))
    merged = merge_buildings(groups)
    unknown = sum(b.building_tag is None for b in merged)
    if progress:
        progress(
            f"  cartografia municipal: {len(merged)} polígonos de {len(sources)} câmara(s); "
            f"{unknown} de tipos que não se mapeiam (escadas, piscinas, ruínas…)"
        )
    crops = [read_fetch_meta(crop_path(config, s)) for s in sources]
    dates = [str(m["fetched_at"]) for m in crops if m.get("fetched_at")]
    write_municipal(
        municipal_path(config),
        merged,
        {
            "fetched_at": min(dates) if dates else None,
            "source": ", ".join(s.name for s in sources),
            "sources": [s.key for s in sources],
            "bbox": list(bbox),
            "n": len(merged),
            "full": False,
            "from_full": all(m.get("from_full") for m in crops),
        },
    )
    mark_cache(municipal_path(config), tiles)
    return merged, sources


# ---------------------------------------------------------------------- muros


def _crop_barriers(
    config: Config,
    src: MunicipalSourceConfig,
    bbox: BBox,
    tiles: list[str],
    force: bool,
    progress: Progress,
) -> list[MunicipalBarrier]:
    path = crop_path(config, src, "barriers")
    stale = force or not path.exists() or not cache_matches(path, tiles)
    full = src.full_barriers_path
    if full_barriers_usable(src):
        if stale or full.stat().st_mtime > path.stat().st_mtime:
            crop_barriers(full, bbox, path, progress)
    elif src.kind == "file":
        fetch_full_municipality(src, progress=progress)
        crop_barriers(full, bbox, path, progress)
    else:
        fetch_municipal_barriers(bbox, src, path, force=stale, progress=progress)
    mark_cache(path, tiles)
    return load_municipal_barriers(path)


def area_barriers(
    config: Config,
    *,
    force: bool = False,
    progress: Progress = None,
) -> Path:
    """Muros das câmaras que cobrem a área, juntos em ``municipal_barriers.geojson``.

    Cada câmara tem as suas camadas por prioridade; entre câmaras manda a ordem do
    config. As chaves levam a câmara à frente (``<câmara>/<camada>:<gid>:<parte>``).
    """
    tiles = config.area.tiles
    bbox = area_bbox(config, tiles)
    sources = [s for s in sources_for_area(config, bbox) if s.barriers]
    if not sources:
        raise MunicipalError("Sem camadas de muros configuradas (barriers) nas câmaras da área")
    found: dict[str, MunicipalBarrier] = {}
    for n, src in enumerate(sources):
        for b in _crop_barriers(config, src, bbox, tiles, force, progress):
            key = f"{src.key}/{b.key}"
            found[key] = MunicipalBarrier(key, b.layer, b.geometry, b.tags, n * 100 + b.priority)
    out = barriers_path(config)
    # Camadas e fonte: as de todas as câmaras (para a aplicação).
    merged_cfg = MunicipalSourceConfig(
        name=", ".join(s.name for s in sources),
        barriers=[layer for s in sources for layer in s.barriers],
    )
    write_barriers(out, found, merged_cfg, bbox)
    mark_cache(out, tiles)
    return out


def area_barrier_layers(config: Config) -> list[str]:
    return [
        layer.label or layer.feature_class
        for s in sources_for_area(config, network=False)
        for layer in s.barriers
    ]


# ------------------------------------------------------------------ estado


def full_status(src: MunicipalSourceConfig) -> dict[str, Any]:
    """Cópia do concelho de uma câmara (edifícios, muros, download interrompido)."""
    from osm3d.acquisition.municipal import BoxLog, barriers_meta, progress_path

    full, walls = src.full_path, src.full_barriers_path
    meta = read_fetch_meta(full)
    wmeta = barriers_meta(walls) if walls.exists() else {}
    parts = [BoxLog.status(progress_path(p)) for p in (full, walls)]
    partial = [s for s in parts if s]
    file_part = None
    if src.kind == "file":
        from osm3d.acquisition.municipal_providers import FileSource

        fs = FileSource(src)
        part = fs.path.with_name(fs.path.name + ".part")
        if fs.remote and part.exists() and not full.exists():
            file_part = part.stat().st_size
    return {
        "cached": full.exists(),
        "path": str(full.resolve()),
        "n": meta.get("n"),
        "fetched_at": meta.get("fetched_at"),
        "bytes": (full.stat().st_size if full.exists() else 0)
        + (walls.stat().st_size if walls.exists() else 0),
        "barriers": {
            "configured": bool(src.barriers),
            "cached": full_barriers_usable(src),
            "n": wmeta.get("n"),
            "fetched_at": wmeta.get("fetched_at"),
        },
        "partial": {
            "done": sum(s["done"] for s in partial),
            "total": sum(s["total"] for s in partial),
        }
        if partial
        else ({"bytes": file_part} if file_part else None),
    }


def sources_status(config: Config) -> list[dict[str, Any]]:
    """Câmaras configuradas, se cobrem a área ativa e o estado da cópia do concelho."""
    try:
        bbox: BBox | None = area_bbox(config)
    except FileNotFoundError:
        bbox = None
    here = {s.key for s in sources_for_area(config, bbox, network=False)} if bbox else set()
    out = []
    for s in config.sources.municipal_sources:
        out.append(
            {
                "key": s.key,
                "name": s.name,
                "kind": s.kind,
                "types": s.types,
                "viewer_url": s.viewer_url or s.layer_url or s.file_url,
                "covers_area": s.key in here,
                "extent_known": source_extent(s, network=False) is not None,
                "barriers": bool(s.barriers),
                "full": full_status(s),
            }
        )
    return out
