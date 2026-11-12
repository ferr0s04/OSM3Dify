"""Gestão dos dados raw na aplicação: quadrículas, ficheiros locais, downloads, limpeza."""

from __future__ import annotations

import os
from collections.abc import Iterable
from typing import Any

from osm3d.acquisition.inventory import (
    PRODUCT_LABELS,
    PRODUCTS,
    REQUIRED_PRODUCTS,
    LocalFile,
    TileIndex,
    TileInfo,
    by_tile,
    delete_local,
    has_product,
    scan_local,
    total_size,
)
from osm3d.app.downloads import ACTIVE, DownloadManager
from osm3d.config import Config
from osm3d.env import load_dotenv
from osm3d.geospatial.tiling import TileKey, tile_rings_wgs84, tiles_in_bbox

# Limite de quadrículas por pedido (mapa e seleção por retângulo): ~5000 km².
MAX_TILES = 5000


class DataService:
    def __init__(self, cfg: Config, downloads: DownloadManager | None = None) -> None:
        self.cfg = cfg
        self.index = TileIndex(cfg.sources.dgt_tile_index)
        self.downloads = downloads or DownloadManager(cfg)

    # ------------------------------------------------------------------ GeoJSON

    def _features(
        self, tiles: list[TileInfo], local: dict[str, list[LocalFile]]
    ) -> list[dict[str, Any]]:
        jobs = {
            t["tile"]: t["status"] for t in self.downloads.state()["tasks"] if t["status"] in ACTIVE
        }
        rings = tile_rings_wgs84(
            [TileKey.from_dgt_name(t.name) for t in tiles], self.cfg.processing_crs
        )
        out = []
        for info, ring in zip(tiles, rings, strict=True):
            files = local.get(info.name, [])
            products = {
                p: {
                    "size": sum(f.size for f in files if f.product == p),
                    "files": [f.path.name for f in files if f.product == p],
                    "partial": not has_product(files, p),
                    # >1: folha (ortofoto) que cobre várias quadrículas.
                    "shared": max((f.shared for f in files if f.product == p), default=1),
                }
                for p in PRODUCTS
                if any(f.product == p for f in files)
            }
            n_req = sum(has_product(files, p) for p in REQUIRED_PRODUCTS)
            out.append(
                {
                    "type": "Feature",
                    "id": int(info.name),
                    "geometry": {"type": "Polygon", "coordinates": [ring]},
                    "properties": {
                        "name": info.name,
                        "lote": info.lote,
                        "disponivel": info.disponivel,
                        "local": products,
                        # complete: LAZ + MDT (o necessário para o pipeline); partial: algo.
                        "state": (
                            "complete"
                            if n_req == len(REQUIRED_PRODUCTS)
                            else "partial"
                            if files
                            else "none"
                        ),
                        "size": sum(f.size for f in files),
                        "job": jobs.get(info.name),
                    },
                }
            )
        return out

    def _collection(self, tiles: list[TileInfo]) -> dict[str, Any]:
        local = by_tile(scan_local(self.cfg.sources))
        return {"type": "FeatureCollection", "features": self._features(tiles, local)}

    def tiles_in_bbox(self, bbox: tuple[float, float, float, float]) -> dict[str, Any]:
        w, s, e, n = bbox
        if not (w < e and s < n):
            raise ValueError("bbox inválida: esperado min_lon,min_lat,max_lon,max_lat")
        n_tiles = len(tiles_in_bbox(bbox, self.cfg.processing_crs))
        if n_tiles > MAX_TILES:
            raise ValueError(
                f"Área demasiado grande ({n_tiles} quadrículas, máx. {MAX_TILES}): aproximar"
            )
        return self._collection(self.index.in_bbox(bbox, self.cfg.processing_crs))

    def tiles(self, names: Iterable[str]) -> dict[str, Any]:
        names = list(dict.fromkeys(names))
        for name in names:
            TileKey.from_dgt_name(name)
        if len(names) > MAX_TILES:
            raise ValueError(f"Demasiadas quadrículas ({len(names)}, máx. {MAX_TILES})")
        return self._collection([self.index.get(n) for n in names])

    # -------------------------------------------------------------------- estado

    def status(self) -> dict[str, Any]:
        files = scan_local(self.cfg.sources)
        local = by_tile(files)
        area_tiles = self.cfg.area.tiles
        load_dotenv()
        sources = self.cfg.sources
        return {
            "index": {
                "available": self.index.available,
                "path": str(self.index.path),
                "n_tiles": len(self.index),
            },
            "area": {
                "name": self.cfg.area.name,
                "bbox": list(self.cfg.area.bbox),
                "tiles": area_tiles,
            },
            "products": [
                {
                    "id": p,
                    "label": PRODUCT_LABELS[p],
                    "collection": sources.dgt_collections.get(p),
                    "required": p in REQUIRED_PRODUCTS,
                    "dir": str(sources.product_dir(p)),
                }
                for p in PRODUCTS
            ],
            "credentials": bool(os.environ.get("DGT_USER") and os.environ.get("DGT_PASSWORD")),
            "local": {
                "type": "FeatureCollection",
                "features": self._features([self.index.get(t) for t in sorted(local)], local),
            },
            "totals": {"tiles": len(local), "bytes": total_size(files)},
            "downloads": self.downloads.state(),
        }

    # ------------------------------------------------------------------- ações

    def download(self, tiles: list[str], products: list[str]) -> dict[str, Any]:
        return self.downloads.submit(tiles, products)

    def delete(
        self, tiles: list[str], products: list[str], include_cache: bool = True
    ) -> dict[str, Any]:
        for t in tiles:
            TileKey.from_dgt_name(t)
        busy = sorted(set(tiles) & self.downloads.busy_tiles())
        if busy:
            raise RuntimeError(f"Downloads em curso para {', '.join(busy)}: cancelar primeiro")
        removed = delete_local(
            self.cfg.sources,
            self.cfg.paths.processed,
            tiles,
            products,
            include_cache=include_cache,
        )
        if removed:
            self.downloads.notify_changed()
        return {
            "deleted": [str(f.path) for f in removed],
            "n_files": len(removed),
            "freed_bytes": sum(f.size for f in removed),
            "tiles": sorted({f.tile for f in removed}),
        }
