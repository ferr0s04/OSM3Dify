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
    ridge_offset: float | None = None  # gabled: posição da cumeeira (fração de b)
    # gabled/hipped em planta não retangular: telhado por straight skeleton sobre o
    # footprint (como nos renderizadores); os parâmetros do retângulo não se usam.
    skeleton: bool = False
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


class PartsLayout(BaseModel):
    """Divisão de um edifício em partes (``building:part``) com vértices partilhados.

    Coordenadas no CRS de processamento, no referencial do **OSM** (sem o deslocamento
    de co-registo). Cada parte é um anel de índices em ``vertices`` (sem repetir o
    primeiro). Partes vizinhas usam os mesmos índices nos vértices comuns, que serão
    os mesmos nós OSM. Os vértices sobre o contorno do edifício são os seus nós, ou
    nós novos inseridos nos lados do contorno.
    """

    vertices: list[tuple[float, float]]
    parts: list[list[int]]
    # Resultado do ajuste de cada parte (mesma ordem de ``parts``); vazio se não ajustado.
    fitted: list[BuildingPart] = Field(default_factory=list)
    source: Literal["suggested", "manual"] = "suggested"

    def polygon(self, i: int) -> Polygon:
        return Polygon([self.vertices[k] for k in self.parts[i]])


class QAFlag(BaseModel):
    code: str
    message: str
    severity: Literal["info", "warning", "error"] = "warning"


ReviewStatus = Literal["pending", "accepted", "rejected"]
GroundChoice = Literal["low", "high"]
# Origem da escolha: terreno plano; pisos; beiral (encaixado); sem evidência; manual.
GroundBasis = Literal["flat", "levels", "eave", "none", "manual"]


class GroundInfo(BaseModel):
    """Cota do terreno à volta do edifício, com os dois candidatos em encosta.

    - ``low``: ponto mais baixo em contacto com o terreno (convenção S3DB literal);
    - ``high``: terreno dominante no pé das paredes (mediana; o "lote" num patamar).

    Com desnível pequeno, os dois coincidem na prática e usa-se ``low``.
    """

    low_m: float
    high_m: float
    step_m: float = Field(description="Desnível no pé das paredes (P95 − P5 dos troços).")
    choice: GroundChoice = "low"
    basis: GroundBasis = "flat"
    reason: str = ""

    def elevation(self, choice: GroundChoice | None = None) -> float:
        return self.high_m if (choice or self.choice) == "high" else self.low_m


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
    ground: GroundInfo | None = None
    registration_offset_m: tuple[float, float] = (0.0, 0.0)
    parts: list[BuildingPart] = Field(default_factory=list)
    # Divisão em partes ativa (vinda da revisão): ``parts`` corresponde a ``layout.parts``.
    layout: PartsLayout | None = None
    # Divisão sugerida pelo pipeline (volume secundário); só é usada se a revisão a aceitar.
    suggested_layout: PartsLayout | None = None
    qa_flags: list[QAFlag] = Field(default_factory=list)
    diagnostics: dict[str, float] = Field(
        default_factory=dict, description="Métricas do processamento (n.º de pontos, etc.)."
    )
    # Revisão humana (ver docs/architecture/frontend-revisao.md §5). Só os edifícios
    # "accepted" são exportados; correções manuais entram nas partes com source=manual.
    review_status: ReviewStatus = "pending"
    review_note: str | None = None
    review_archived: bool = False  # aceite já submetido / rejeitado arquivado

    @property
    def key(self) -> str:
        return f"{self.osm_type}/{self.osm_id}"

    @property
    def is_single_part(self) -> bool:
        return len(self.parts) == 1

    @property
    def needs_review(self) -> bool:
        return any(f.severity != "info" for f in self.qa_flags)
