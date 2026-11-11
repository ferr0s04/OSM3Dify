"""Carregamento e validação da configuração YAML."""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, Field

from osm3d.domain.models import RoofShape


class AreaConfig(BaseModel):
    name: str
    bbox: tuple[float, float, float, float] = Field(
        description="WGS84: (min_lon, min_lat, max_lon, max_lat)"
    )
    osm_relation_id: int | None = Field(
        default=None,
        description="Relação OSM do limite administrativo; filtra edifícios pelo polígono.",
    )


class PathsConfig(BaseModel):
    raw: Path = Path("data/raw")
    processed: Path = Path("data/processed")
    output: Path = Path("data/output")
    reference: Path = Path("data/reference")


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
    lidar_dir: Path = Path("data/raw/lidar")
    dtm_dir: Path = Path("data/raw/dtm")
    orthophoto_dir: Path = Path("data/raw/orthophotos")


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
        RoofShape.PYRAMIDAL,
    ]
    flat_max_slope_deg: float = 5.0
    min_shape_confidence: float = Field(default=0.7, ge=0, le=1)
    max_rmse_m: float = 0.35


class AppearanceConfig(BaseModel):
    enabled: bool = False
    colour_palette: list[str] = []
    min_colour_confidence: float = Field(default=0.6, ge=0, le=1)


class ExportConfig(BaseModel):
    height_rounding_m: float = 0.5
    roof_height_rounding_m: float = 0.5
    overwrite_existing_tags: bool = False
    export_parts_to_osm: bool = False


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
