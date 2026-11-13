"""Carregamento e validação da configuração YAML."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator

from osm3d.domain.models import RoofShape

if TYPE_CHECKING:
    from shapely.geometry.base import BaseGeometry

    from osm3d.geospatial.tiling import TileKey


class AreaConfig(BaseModel):
    """Área de estudo = um conjunto de quadrículas LiDAR de 1 km (grelha DGT).

    As mesmas quadrículas definem o LAZ/MDT a descarregar e os edifícios OSM a
    processar (os que têm o centróide dentro delas). Não há outra definição de área.
    """

    # Usado em caminhos (data/processed/<nome>): só minúsculas, dígitos, "-" e "_".
    name: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,62}$")
    label: str | None = Field(default=None, max_length=200, description="Nome a mostrar.")
    tiles: list[str] = Field(min_length=1, description="Identificadores DGT (p.ex. 155480).")

    @model_validator(mode="before")
    @classmethod
    def _bbox_to_tiles(cls, data: Any) -> Any:
        # Compatibilidade: uma bbox WGS84 antiga é convertida nas quadrículas que interseta.
        if isinstance(data, dict) and "tiles" not in data and "bbox" in data:
            from osm3d.geospatial.tiling import tiles_in_bbox

            w, s, e, n = (float(v) for v in data["bbox"])
            data = {k: v for k, v in data.items() if k not in ("bbox", "osm_relation_id")}
            data["tiles"] = [k.dgt_name for k in tiles_in_bbox((w, s, e, n))]
        return data

    @field_validator("tiles", mode="before")
    @classmethod
    def _check_tiles(cls, value: Any) -> list[str]:
        tiles = [str(t).strip() for t in value]  # no YAML vêm como números
        bad = [t for t in tiles if not (len(t) == 6 and t.isdigit())]
        if bad:
            raise ValueError(f"Identificadores DGT inválidos: {', '.join(bad[:5])}")
        return sorted(set(tiles))

    @property
    def tile_keys(self) -> list[TileKey]:
        from osm3d.geospatial.tiling import TileKey

        return [TileKey.from_dgt_name(t) for t in self.tiles]

    def polygon(self) -> BaseGeometry:
        """União das quadrículas (EPSG:3763)."""
        import shapely

        return shapely.union_all([shapely.box(*k.bounds) for k in self.tile_keys])

    @property
    def bbox(self) -> tuple[float, float, float, float]:
        """Caixa envolvente WGS84 das quadrículas (min_lon, min_lat, max_lon, max_lat)."""
        from osm3d.geospatial.tiling import tile_rings_wgs84

        pts = [p for ring in tile_rings_wgs84(self.tile_keys) for p in ring]
        lons, lats = [p[0] for p in pts], [p[1] for p in pts]
        return (min(lons), min(lats), max(lons), max(lats))


class PathsConfig(BaseModel):
    raw: Path = Path("data/raw")
    processed: Path = Path("data/processed")
    output: Path = Path("data/output")
    reference: Path = Path("data/reference")
    # Registo das áreas criadas na aplicação e da área ativa (omissão: data/areas.json).
    areas_file: Path | None = None

    @property
    def areas_path(self) -> Path:
        return self.areas_file or self.processed.parent / "areas.json"


class MunicipalSourceConfig(BaseModel):
    """Cartografia municipal de edifícios (geoportal MapGuide), para os candidatos.

    Ver ``acquisition/municipal.py``. Só com autorização do município.
    """

    enabled: bool = False
    name: str = "Câmara Municipal de Vila do Conde"
    viewer_url: str = "https://sig.cm-viladoconde.pt/geoportal"
    mapagent_url: str = "https://sig.cm-viladoconde.pt/mapguide/mapagent/mapagent.fcgi"
    feature_source: str = "Library://geoportal/Data/pg_viladoconde.FeatureSource"
    feature_class: str = "cartografia_simplificada:muni_5000_h_planimetria_pg"
    filter: str = "objeto = 'const_poligonos'"
    chunk_m: float = Field(default=250.0, gt=10, le=1000)
    delay_s: float = Field(default=0.5, ge=0.2, description="Pausa entre pedidos.")
    min_area_m2: float = Field(default=8.0, ge=0)


class SourcesConfig(BaseModel):
    overpass_url: str = "https://overpass-api.de/api/interpreter"
    dgt_stac_url: str | None = None
    dgt_collections: dict[str, str] = Field(
        default_factory=lambda: {
            "lidar": "LAZ",
            "dtm": "MDT-50cm",
            "dsm": "MDS-50cm",
            "orthophoto": "ORTOS-2025",
        }
    )
    dgt_delay_s: float = Field(default=2.0, ge=0.5, description="Pausa entre pedidos à DGT.")
    # Grelha oficial de quadrículas LiDAR (NAME, LOTE, DISPONIVEL); opcional.
    dgt_tile_index: Path = Path("data/raw/dgt/LiDAR2024_2025_Seccionamento.gpkg")
    lidar_dir: Path = Path("data/raw/lidar")
    dtm_dir: Path = Path("data/raw/dtm")
    dsm_dir: Path = Path("data/raw/dsm")
    orthophoto_dir: Path = Path("data/raw/orthophotos")
    municipal: MunicipalSourceConfig = MunicipalSourceConfig()

    def product_dir(self, product: str) -> Path:
        dirs = {
            "lidar": self.lidar_dir,
            "dtm": self.dtm_dir,
            "dsm": self.dsm_dir,
            "orthophoto": self.orthophoto_dir,
        }
        if product not in dirs:
            raise ValueError(f"Produto desconhecido: {product} (opções: {', '.join(dirs)})")
        return dirs[product]


class FootprintsConfig(BaseModel):
    min_area_m2: float = 20.0
    registration_max_shift_m: float = 3.0
    registration_step_m: float = 0.25


class HeightsConfig(BaseModel):
    building_classes: list[int] = [6]
    excluded_classes: list[int] = [3, 4, 5, 7]
    roof_percentile: float = Field(default=95, ge=50, le=100)
    ground_ring_buffer_m: float = 2.0
    ground_percentile: float = Field(default=5, ge=0, le=50)
    ndsm_resolution_m: float = 0.5
    min_points_per_building: int = 50


class RoofsConfig(BaseModel):
    candidate_shapes: list[RoofShape] = [
        RoofShape.FLAT,
        RoofShape.SKILLION,
        RoofShape.GABLED,
        RoofShape.HIPPED,
        RoofShape.SIDE_HIPPED,
        RoofShape.PYRAMIDAL,
        RoofShape.ROUND,
    ]
    flat_max_slope_deg: float = 5.0
    min_shape_confidence: float = Field(default=0.7, ge=0, le=1)
    max_rmse_m: float = 0.20


class AppearanceConfig(BaseModel):
    enabled: bool = True
    min_colour_confidence: float = Field(default=0.6, ge=0, le=1)


class ExportConfig(BaseModel):
    height_rounding_m: float = 0.5
    roof_height_rounding_m: float = 0.5
    overwrite_existing_tags: bool = False
    export_parts_to_osm: bool = False
    # Incluir edifícios ainda não revistos (sem sinalizações). A revisão é então feita
    # no JOSM; com false, só os "accepted" (aplicação de revisão) são exportados.
    include_pending: bool = False
    # "true": upload normal no JOSM; "false": JOSM avisa; "never": JOSM recusa o upload.
    josm_upload: Literal["never", "false", "true"] = "true"


class Config(BaseModel):
    area: AreaConfig
    processing_crs: str = "EPSG:3763"
    paths: PathsConfig = PathsConfig()
    sources: SourcesConfig = SourcesConfig()
    footprints: FootprintsConfig = FootprintsConfig()
    heights: HeightsConfig = HeightsConfig()
    roofs: RoofsConfig = RoofsConfig()
    appearance: AppearanceConfig = AppearanceConfig()
    export: ExportConfig = ExportConfig()


def load_config(path: Path) -> Config:
    with path.open(encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}
    return Config.model_validate(raw)
