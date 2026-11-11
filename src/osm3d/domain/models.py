"""Modelo intermédio: Building → BuildingPart → Roof.

Convenções (alinhadas com Simple 3D Buildings):

- Geometrias em CRS métrico de processamento (EPSG:3763 por omissão).
- ``BuildingPart.height`` é medida do chão ao **topo do telhado** (inclui o telhado),
  como a tag OSM ``height``. ``Roof.height`` é a parte dessa altura ocupada pelo telhado.
- Azimutes em graus, 0 = norte, sentido horário.
- Cada atributo estimado leva valor, confiança [0, 1] e proveniência (``Estimate``).
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Any, Literal

import shapely
from pydantic import BaseModel, ConfigDict, Field, PlainSerializer, PlainValidator
from shapely.geometry import MultiPolygon, Polygon
from shapely.geometry.base import BaseGeometry


def _parse_geometry(value: Any) -> BaseGeometry:
    if isinstance(value, BaseGeometry):
        geom = value
    elif isinstance(value, str):
        geom = shapely.from_wkt(value)
    else:
        raise TypeError(f"Geometria inválida: {type(value).__name__}")
    if not isinstance(geom, Polygon | MultiPolygon):
        raise ValueError(f"Esperado Polygon/MultiPolygon, obtido {geom.geom_type}")
    return geom


Geometry = Annotated[
    BaseGeometry,
    PlainValidator(_parse_geometry),
    PlainSerializer(lambda g: g.wkt, return_type=str),
]


class RoofShape(StrEnum):
    FLAT = "flat"
    SKILLION = "skillion"
    GABLED = "gabled"
    HIPPED = "hipped"
    PYRAMIDAL = "pyramidal"
    UNKNOWN = "unknown"


class Source(StrEnum):
    LIDAR = "lidar"
    NDSM = "ndsm"
    ORTHOPHOTO = "orthophoto"
    OSM = "osm"
    MANUAL = "manual"


class Estimate[T](BaseModel):
    """Valor estimado com confiança e proveniência."""

    model_config = ConfigDict(frozen=True)

    value: T
    confidence: float = Field(ge=0.0, le=1.0)
    source: Source


class Roof(BaseModel):
    shape: Estimate[RoofShape]
    height: Estimate[float] | None = Field(
        default=None, description="Altura do telhado (beiral → cumeeira), metros."
    )
    ridge_azimuth_deg: float | None = Field(
        default=None,
        ge=0.0,
        lt=180.0,
        description="Direção da cumeeira (gabled/hipped), eixo em [0, 180).",
    )
    facing_azimuth_deg: float | None = Field(
        default=None,
        ge=0.0,
        lt=360.0,
        description="Para onde a água 'olha' (sentido descendente) — skillion.",
    )
    colour: Estimate[str] | None = None
    rmse_m: float | None = Field(default=None, description="Resíduo do ajuste do modelo.")


class BuildingPart(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    id: str
    geometry: Geometry
    height: Estimate[float]
    min_height: float = Field(default=0.0, ge=0.0)
    roof: Roof
    facade_colour: Estimate[str] | None = None


class QAFlag(BaseModel):
    code: str
    message: str
    severity: Literal["info", "warning", "error"] = "warning"


class Building(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    osm_type: Literal["way", "relation"]
    osm_id: int
    osm_version: int = Field(description="Versão OSM no download — usada para detetar conflitos.")
    footprint: Geometry
    existing_tags: dict[str, str] = Field(default_factory=dict)
    ground_elevation_m: float | None = None
    registration_offset_m: tuple[float, float] = (0.0, 0.0)
    parts: list[BuildingPart] = Field(default_factory=list)
    qa_flags: list[QAFlag] = Field(default_factory=list)

    @property
    def is_single_part(self) -> bool:
        return len(self.parts) == 1

    @property
    def needs_review(self) -> bool:
        return any(f.severity != "info" for f in self.qa_flags)
