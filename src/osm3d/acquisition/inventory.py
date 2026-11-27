"""Ficheiros raw por quadrícula: grelha oficial da DGT, inventário local e limpeza.

A grelha (``LiDAR2024_2025_Seccionamento.gpkg``, camada ``tiles``) tem uma linha por
quadrícula de 1 km com ``NAME`` (identificador DGT), ``LOTE`` e ``DISPONIVEL``. A
geometria não é lida: as quadrículas estão todas alinhadas a 1000 m em EPSG:3763 e
saem do identificador (``TileKey.from_dgt_name``). ``DISPONIVEL`` é só informativo:
na versão de 04/07/2025 está desatualizado (ver ``docs/validacao.md``).
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from osm3d.acquisition.dgt import tile_from_id
from osm3d.config import SourcesConfig
from osm3d.geospatial.tiling import TileKey, tiles_in_bbox

log = logging.getLogger(__name__)

PRODUCTS: tuple[str, ...] = ("lidar", "dtm", "dsm", "orthophoto")
# Necessários para o pipeline (fases roofs/colours/candidates); os outros são opcionais.
REQUIRED_PRODUCTS: tuple[str, ...] = ("lidar", "dtm")
PRODUCT_LABELS = {
    "lidar": "LiDAR (LAZ)",
    "dtm": "MDT 50 cm",
    "dsm": "MDS 50 cm",
    "orthophoto": "Ortofotos",
}
SUFFIXES: dict[str, tuple[str, ...]] = {
    "lidar": (".laz", ".las"),
    "dtm": (".tif", ".tiff"),
    "dsm": (".tif", ".tiff"),
    "orthophoto": (".tif", ".tiff", ".jp2"),
}
PARTIAL_SUFFIX = ".part"


# ----------------------------------------------------------------- grelha oficial


@dataclass(frozen=True)
class TileInfo:
    name: str
    lote: int | None = None
    disponivel: str | None = None  # "sim" / "nao" (desatualizado; só informativo)


class TileIndex:
    """Grelha oficial de quadrículas LiDAR, carregada a pedido (só atributos)."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._tiles: dict[str, TileInfo] | None = None
        self._lock = threading.Lock()

    @property
    def available(self) -> bool:
        return self.path.exists()

    def _load(self) -> dict[str, TileInfo]:
        with self._lock:
            if self._tiles is None:
                import pyogrio

                df = pyogrio.read_dataframe(
                    self.path, columns=["NAME", "LOTE", "DISPONIVEL"], read_geometry=False
                )
                self._tiles = {
                    str(name): TileInfo(
                        str(name),
                        int(lote) if lote == lote else None,  # NaN → None
                        str(disp) if isinstance(disp, str) else None,
                    )
                    for name, lote, disp in zip(
                        df["NAME"], df["LOTE"], df["DISPONIVEL"], strict=True
                    )
                }
                log.info("Grelha DGT: %d quadrículas (%s)", len(self._tiles), self.path)
            return self._tiles

    def __len__(self) -> int:
        return len(self._load()) if self.available else 0

    def get(self, name: str) -> TileInfo:
        """Atributos de uma quadrícula (sem grelha ou fora dela: só o nome)."""
        if not self.available:
            return TileInfo(name)
        return self._load().get(name, TileInfo(name))

    def __contains__(self, name: object) -> bool:
        return not self.available or name in self._load()

    def in_bbox(
        self, bbox_wgs84: tuple[float, float, float, float], crs: str = "EPSG:3763"
    ) -> list[TileInfo]:
        """Quadrículas da grelha que intersetam a bbox (sem grelha: toda a malha de 1 km)."""
        keys = tiles_in_bbox(bbox_wgs84, crs)
        if not self.available:
            return [TileInfo(k.dgt_name) for k in keys]
        tiles = self._load()
        return [tiles[k.dgt_name] for k in keys if k.dgt_name in tiles]


# ------------------------------------------------------------------ ficheiros locais


@dataclass(frozen=True)
class LocalFile:
    tile: str
    product: str  # um de PRODUCTS, ou "cache" (pontos .npz da aplicação de revisão)
    path: Path
    size: int
    partial: bool = False  # download interrompido (.part)
    # Ficheiro de uma folha que cobre várias quadrículas (ortofotos): n.º de quadrículas.
    shared: int = 1


# Produtos que a DGT distribui noutra grelha (folhas maiores do que as quadrículas).
SHEET_PRODUCTS = frozenset({"orthophoto"})
_sheet_cache: dict[tuple[str, float, int], list[str]] = {}


def sheet_tiles(path: Path) -> list[str]:
    """Quadrículas de 1 km cobertas por um raster (limites lidos do cabeçalho)."""
    st = path.stat()
    key = (str(path), st.st_mtime, st.st_size)
    if key not in _sheet_cache:
        import rasterio
        from rasterio.warp import transform_bounds

        from osm3d.geospatial.tiling import TILE_SIZE_M, tile_of

        try:
            with rasterio.open(path) as src:
                x0, y0, x1, y1 = src.bounds
                if src.crs and src.crs.to_epsg() != 3763:
                    x0, y0, x1, y1 = transform_bounds(src.crs, "EPSG:3763", x0, y0, x1, y1)
        except (rasterio.errors.RasterioError, OSError) as exc:
            log.warning("Não foi possível ler %s: %s", path, exc)
            return []
        lo, hi = tile_of(x0 + 1, y0 + 1), tile_of(x1 - 1, y1 - 1)  # margem: sem vizinhas
        _sheet_cache[key] = [
            TileKey(ix, iy).dgt_name
            for ix in range(lo.ix, hi.ix + 1)
            for iy in range(lo.iy, hi.iy + 1)
            # Só quadrículas com uma parte relevante (> 1 %) dentro da folha.
            if (min(x1, (ix + 1) * TILE_SIZE_M) - max(x0, ix * TILE_SIZE_M))
            * (min(y1, (iy + 1) * TILE_SIZE_M) - max(y0, iy * TILE_SIZE_M))
            > 0.01 * TILE_SIZE_M**2
        ]
    return _sheet_cache[key]


def _product_files(path: Path, product: str) -> list[LocalFile]:
    """Entradas do inventário para ``path`` (uma por quadrícula coberta)."""
    name = path.name
    partial = name.endswith(PARTIAL_SUFFIX)
    base = name[: -len(PARTIAL_SUFFIX)] if partial else name
    if Path(base).suffix.lower() not in SUFFIXES[product]:
        return []
    size = path.stat().st_size
    if tile := tile_from_id(base):
        return [LocalFile(tile, product, path, size, partial)]
    if product in SHEET_PRODUCTS and not partial:
        tiles = sheet_tiles(path)
        return [LocalFile(t, product, path, size, False, len(tiles)) for t in tiles]
    return []


def scan_local(sources: SourcesConfig, products: Iterable[str] = PRODUCTS) -> list[LocalFile]:
    """Todos os ficheiros DGT descarregados (todas as versões e ``.part``).

    Um ficheiro de uma folha (ortofotos) aparece uma vez por quadrícula que cobre.
    """
    found: list[LocalFile] = []
    for product in products:
        directory = sources.product_dir(product)
        if not directory.is_dir():
            continue
        for path in sorted(directory.iterdir()):
            if path.is_file():
                found.extend(_product_files(path, product))
    return found


def total_size(files: Iterable[LocalFile]) -> int:
    """Bytes em disco (um ficheiro partilhado por várias quadrículas conta uma vez)."""
    return sum({f.path: f.size for f in files}.values())


def by_tile(files: Iterable[LocalFile]) -> dict[str, list[LocalFile]]:
    out: dict[str, list[LocalFile]] = {}
    for f in files:
        out.setdefault(f.tile, []).append(f)
    return out


def has_product(files: Iterable[LocalFile], product: str) -> bool:
    return any(f.product == product and not f.partial for f in files)


def point_caches(processed_root: Path, lidar_file: Path) -> list[Path]:
    """Caches ``.npz`` de pontos de um LAZ: a da aplicação de revisão, por área
    (``<processed>/<área>/cache/<nome>.npz``), e a das fases, partilhada
    (``<processed>/lidar_cache/<nome>.c<classes>.npz``)."""
    if not processed_root.is_dir():
        return []
    review = [
        area / "cache" / f"{lidar_file.stem}.npz"
        for area in sorted(processed_root.iterdir())
        if (area / "cache" / f"{lidar_file.stem}.npz").is_file()
    ]
    stages = processed_root / "lidar_cache"
    shared = sorted(stages.glob(f"{lidar_file.stem}.c*.npz")) if stages.is_dir() else []
    return review + shared


def delete_local(
    sources: SourcesConfig,
    processed_root: Path,
    tiles: Iterable[str],
    products: Iterable[str] = PRODUCTS,
    *,
    include_cache: bool = True,
) -> list[LocalFile]:
    """Apaga os ficheiros raw das quadrículas indicadas e devolve o que foi apagado.

    Só toca em ficheiros DGT reconhecidos nas pastas de cada produto (nunca na grelha
    nem noutros ficheiros). Com ``include_cache``, apaga também as caches de pontos
    derivadas dos LAZ apagados, que de outro modo continuariam a ser usadas.

    Uma folha partilhada (ortofotos) é apagada se cobrir alguma das quadrículas, e
    deixa assim de existir também para as outras quadrículas que cobre.
    """
    wanted_tiles, wanted_products = set(tiles), set(products)
    unknown = wanted_products - set(PRODUCTS)
    if unknown:
        raise ValueError(f"Produto desconhecido: {', '.join(sorted(unknown))}")
    removed: list[LocalFile] = []
    done: set[Path] = set()
    for f in scan_local(sources, sorted(wanted_products)):
        if f.tile not in wanted_tiles or f.path in done:
            continue
        done.add(f.path)
        caches = point_caches(processed_root, f.path) if f.product == "lidar" else []
        f.path.unlink(missing_ok=True)
        removed.append(f)
        log.info("Apagado: %s", f.path)
        if include_cache and not f.partial:
            for cache in caches:
                size = cache.stat().st_size
                cache.unlink(missing_ok=True)
                removed.append(LocalFile(f.tile, "cache", cache, size))
                log.info("Apagado: %s", cache)
    return removed
