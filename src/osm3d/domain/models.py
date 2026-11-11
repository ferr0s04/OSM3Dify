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
    SIDE_HIPPED = "side_hipped"  # três águas: hip numa ponta, empena na outra
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


class RoofModel(BaseModel):
    """Parâmetros exatos do modelo paramétrico ajustado, para gerar a geometria 3D.

    Coordenadas no referencial do LiDAR (footprint já deslocado pelo co-registo).
    Não é exportado para o OSM: serve a visualização e a revisão.
    """

    model_config = ConfigDict(frozen=True)

    cx: float
    cy: float
    azimuth_deg: float  # eixo u do modelo (cumeeira em gabled/hipped)
    half_length: float
    half_width: float
    flip: bool = False  # skillion: água virada para −v; side_hipped: hip em −u
    ridge_ratio: float | None = None  # hipped/side_hipped: cumeeira / comprimento
    eave_z: float  # cota absoluta do beiral
    roof_height: float


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
    model: RoofModel | None = None


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


ReviewStatus = Literal["pending", "accepted", "rejected"]


class Building(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    osm_type: Literal["way", "relation"]
    osm_id: int
    osm_version: int = Field(description="Versão OSM no download — usada para detetar conflitos.")
    footprint: Geometry
    existing_tags: dict[str, str] = Field(default_factory=dict)
    osm_parts: list[int] = Field(
        default_factory=list, description="Ids das ways building:part já mapeadas no contorno."
    )
    ground_elevation_m: float | None = None
    registration_offset_m: tuple[float, float] = (0.0, 0.0)
    parts: list[BuildingPart] = Field(default_factory=list)
    qa_flags: list[QAFlag] = Field(default_factory=list)
    diagnostics: dict[str, float] = Field(
        default_factory=dict, description="Métricas do processamento (n.º de pontos, etc.)."
    )
    # Revisão humana (ver docs/architecture/frontend-revisao.md §5). Só os edifícios
    # "accepted" são exportados; correções manuais entram nas partes com source=manual.
    review_status: ReviewStatus = "pending"
    review_note: str | None = None

    @property
    def key(self) -> str:
        return f"{self.osm_type}/{self.osm_id}"

    @property
    def is_single_part(self) -> bool:
        return len(self.parts) == 1

    @property
    def needs_review(self) -> bool:
        return any(f.severity != "info" for f in self.qa_flags)
