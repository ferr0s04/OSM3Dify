"""Carregamento e validação da configuração YAML."""

from __future__ import annotations

import re
import unicodedata
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator

from osm3d.domain.models import RoofShape

if TYPE_CHECKING:
    from shapely.geometry.base import BaseGeometry

    from osm3d.geospatial.tiling import TileKey


# Extensão da grelha LiDAR da DGT (Portugal continental), WGS84.
MAINLAND_PORTUGAL_WGS84 = (-9.6, 36.9, -6.1, 42.2)


class AreaConfig(BaseModel):
    """Área de estudo = um conjunto de quadrículas LiDAR de 1 km (grelha DGT).

    As mesmas quadrículas definem o LAZ/MDT a descarregar e os edifícios OSM a
    processar (os que têm o centróide dentro delas). Não há outra definição de área.
    Sem quadrículas (área inicial por omissão), escolhem-se na aplicação; o pipeline
    recusa-se a correr até lá.
    """

    # Usado em caminhos (data/processed/<nome>): só minúsculas, dígitos, "-" e "_".
    name: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,62}$")
    # Nome escrito pelo utilizador. Sem ele, mostra-se o das freguesias cobertas.
    label: str | None = Field(default=None, max_length=200, description="Nome a mostrar.")
    tiles: list[str] = Field(
        default_factory=list, description="Identificadores DGT (p.ex. 155480)."
    )
    # Freguesias cobertas (da mais à menos representativa) e as quadrículas para que
    # foram calculadas: mudando as quadrículas, voltam a calcular-se.
    places: list[str] = Field(default_factory=list)
    places_tiles: list[str] = Field(default_factory=list)

    @property
    def places_current(self) -> bool:
        return bool(self.places) and self.places_tiles == self.tiles

    @property
    def display_label(self) -> str | None:
        """O nome escrito; senão, as freguesias ("Azurara, Árvore e Vila do Conde")."""
        if self.label:
            return self.label
        if self.places_current:
            from osm3d.acquisition.places import places_label

            return places_label(self.places)
        return None

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
        """Caixa envolvente WGS84 das quadrículas (min_lon, min_lat, max_lon, max_lat).

        Sem quadrículas: Portugal continental (onde há LiDAR da DGT), para enquadrar o mapa.
        """
        from osm3d.geospatial.tiling import tile_rings_wgs84

        if not self.tiles:
            return MAINLAND_PORTUGAL_WGS84
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

    @property
    def lidar_cache(self) -> Path:
        """Pontos dos LAZ já descomprimidos (partilhados por todas as áreas)."""
        return self.processed / "lidar_cache"


class BarrierRule(BaseModel):
    """Linhas da camada com estes atributos (todos) → estas tags OSM."""

    match: dict[str, str] = Field(default_factory=dict)
    tags: dict[str, str]


class BarrierLayer(BaseModel):
    """Camada de muros/vedações do geoportal (linhas). A primeira regra que bate manda.

    ``feature_class``: no MapGuide, a classe (esquema:classe); no ArcGIS, o endereço da
    camada (…/MapServer/<n>); num ficheiro, o nome da camada.
    """

    label: str = ""  # p.ex. "cartografia 2024" (mostrado na aplicação)
    feature_class: str
    filter: str = ""
    rules: list[BarrierRule] = Field(default_factory=list)

    def tags_for(self, attrs: dict[str, str | None]) -> dict[str, str] | None:
        for rule in self.rules:
            if all((attrs.get(k) or "") == v for k, v in rule.match.items()):
                return dict(rule.tags) or None
        return None


MunicipalKind = Literal["mapguide", "arcgis", "file"]
# Classificação dos tipos de edifício: "catalogo" (descrições do catálogo de objetos da
# cartografia 1:1000–1:10000 anterior ao CartTop, p.ex. "Vivenda Casa", "06020103-PRÉDIO")
# ou "carttop" (modelo CartTop da DGT: utilização, forma e condição do edifício).
TypeSchema = Literal["catalogo", "carttop"]


class MunicipalSourceConfig(BaseModel):
    """Cartografia municipal de edifícios (e muros) de um concelho, para os candidatos.

    Ver ``acquisition/municipal.py`` e ``acquisition/municipal_providers.py``. Só com
    autorização do município. Três tipos de fonte (``kind``):

    - ``mapguide``: geoportal Autodesk MapGuide (``viewer_url``, ``mapagent_url``,
      ``feature_source``, ``feature_class``, ``filter``);
    - ``arcgis``: serviço ArcGIS REST (``layer_url`` = …/MapServer/<n>; ``type_field``);
    - ``file``: ficheiro (GeoPackage) de dados abertos (``file_url``, ``layer``).
    """

    kind: MunicipalKind = "mapguide"
    id: str = ""  # identificador curto (omissão: do nome)
    enabled: bool = False
    name: str = ""  # p.ex. "Câmara Municipal de …" (mostrado na aplicação)
    viewer_url: str = ""  # página do visualizador / do geoportal (ligação na aplicação)
    mapagent_url: str = ""  # …/mapguide/mapagent/mapagent.fcgi
    feature_source: str = ""  # Library://…/….FeatureSource
    feature_class: str = ""  # esquema:classe da camada de edifícios
    filter: str = ""  # filtro dos polígonos de edifício (MapGuide/ArcGIS; vazio: todos)
    layer_url: str = ""  # ArcGIS: …/MapServer/<n> ou …/FeatureServer/<n>
    file_url: str = ""  # ficheiro: endereço (http/https) ou caminho local
    layer: str = ""  # ficheiro: camada dos edifícios
    type_field: str = ""  # campo do tipo (omissão: descr / layer / campos CartTop)
    types: TypeSchema = "catalogo"  # classificação dos tipos de edifício (ver TypeSchema)
    # Extensão do concelho (EPSG:3763): escolhe as câmaras de cada área sem pedidos.
    # Omissão: a da cópia do concelho ou a pedida uma vez à fonte (em cache).
    bbox: tuple[float, float, float, float] | None = None
    chunk_m: float = Field(default=250.0, gt=10, le=1000)
    delay_s: float = Field(default=0.5, ge=0.2, description="Pausa entre pedidos.")
    min_area_m2: float = Field(default=8.0, ge=0)
    # Cópia de todo o concelho (descarregada uma vez, a pedido): as áreas recortam-na
    # localmente em vez de pedirem ao geoportal. Pedidos de 1 km (~700 polígonos cada).
    cache_dir: Path = Path("data/raw/municipal")
    full_chunk_m: float = Field(default=1000.0, gt=10, le=1000)
    # Muros e vedações (linhas), para as propostas de barrier=wall. Por prioridade: onde
    # duas camadas têm o mesmo muro, fica o da primeira (p.ex. a cartografia mais recente).
    barriers: list[BarrierLayer] = Field(default_factory=list)

    @property
    def configured(self) -> bool:
        """Há uma fonte configurada (endereços e camada)?"""
        if self.kind == "arcgis":
            return bool(self.layer_url)
        if self.kind == "file":
            return bool(self.file_url and self.layer)
        return all((self.viewer_url, self.mapagent_url, self.feature_source, self.feature_class))

    @model_validator(mode="after")
    def _needs_source(self) -> MunicipalSourceConfig:
        if self.enabled and not self.configured:
            needs = {
                "mapguide": "viewer_url, mapagent_url, feature_source e feature_class",
                "arcgis": "layer_url",
                "file": "file_url e layer",
            }[self.kind]
            raise ValueError(f"sources.municipal ({self.name or self.kind}): enabled exige {needs}")
        return self

    @property
    def key(self) -> str:
        """Identificador curto e estável (nomes de ficheiros, aplicação)."""
        text = self.id or self.name
        ascii_name = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
        slug = re.sub(r"[^a-z0-9]+", "-", ascii_name.lower()).strip("-")
        return slug or "municipal"

    @property
    def full_path(self) -> Path:
        """Cópia de todo o concelho (GeoJSON, EPSG:3763)."""
        ascii_name = unicodedata.normalize("NFKD", self.name).encode("ascii", "ignore").decode()
        slug = re.sub(r"[^a-z0-9]+", "-", ascii_name.lower())
        return self.cache_dir / f"{slug.strip('-') or self.key}.geojson"

    @property
    def full_barriers_path(self) -> Path:
        """Muros e vedações de todo o concelho (GeoJSON, EPSG:3763), ao lado dos edifícios."""
        return self.full_path.with_name(self.full_path.stem + "-muros.geojson")


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
    # Cartografia municipal: uma câmara (``municipal``, a forma antiga) e/ou várias
    # (``municipalities``). Cada área usa as câmaras cuja extensão a cobre.
    municipal: MunicipalSourceConfig = MunicipalSourceConfig()
    municipalities: list[MunicipalSourceConfig] = Field(default_factory=list)

    @property
    def municipal_sources(self) -> list[MunicipalSourceConfig]:
        """Câmaras configuradas (a de ``municipal`` primeiro), sem repetidas."""
        out = [self.municipal] if self.municipal.configured else []
        for m in self.municipalities:
            if m.configured and all(o.key != m.key for o in out):
                out.append(m)
        return out

    def municipal_source(self, key: str) -> MunicipalSourceConfig:
        for m in self.municipal_sources:
            if m.key == key:
                return m
        raise KeyError(f"Câmara não configurada: {key}")

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
    # Edifícios encostados (geminados, em banda) com o mesmo telhado: as pequenas
    # diferenças de height/roof:height entre eles são ruído do ajuste e igualam-se.
    attached_harmonize: bool = True
    attached_min_shared_m: float = Field(default=2.0, ge=0)  # parede comum mínima
    # Diferença máxima dentro de um grupo (abaixo do arredondamento da exportação, 0,5 m:
    # alturas que o LiDAR vê como diferentes ficam como estão).
    attached_height_tolerance_m: float = Field(default=0.35, ge=0)
    attached_roof_height_tolerance_m: float = Field(default=0.6, ge=0)
    # "Mesmo telhado": o modelo do vizinho explica os pontos deste com no máximo este
    # aumento do erro (m, descontado o desnível entre os dois).
    attached_max_shape_increase_m: float = Field(default=0.03, ge=0)


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
    # false: não acrescentar nem alterar roof:colour com a cor estimada (o do OSM, se
    # houver, fica; uma cor escolhida à mão na revisão é exportada na mesma).
    roof_colour: bool = True
    export_parts_to_osm: bool = False
    # Incluir edifícios ainda não revistos (sem sinalizações). A revisão é então feita
    # no JOSM; com false, só os "accepted" (aplicação de revisão) são exportados.
    include_pending: bool = False
    # "true": upload normal no JOSM; "false": JOSM avisa; "never": JOSM recusa o upload.
    # Por omissão "false": o JOSM avisa antes do upload, para quem usa a ferramenta
    # confirmar que pode contribuir os dados para o OSM (ver README, "Licenças e
    # responsabilidade"); "true" no local.yaml tira o aviso.
    josm_upload: Literal["never", "false", "true"] = "false"


class TopologyConfig(BaseModel):
    """Edifícios novos vs. o que já está no OSM (ver osm3d.validation.topology)."""

    # Folga às vias, limites de landuse e edifícios do OSM depois de um recorte.
    clearance_m: float = Field(default=0.1, ge=0.0, le=1.0)
    # Só se recortam lascas: nada mais largo do que isto, até esta fração da área.
    max_trim_m: float = Field(default=1.0, ge=0.0, le=5.0)
    max_trim_fraction: float = Field(default=0.15, ge=0.0, le=0.5)
    # Encaixar nos limites de landuse (os edifícios e as vias são sempre verificados).
    landuse: bool = True


class Config(BaseModel):
    # Área inicial. Sem ``area`` no YAML: sem quadrículas, escolhidas na aplicação.
    area: AreaConfig = Field(default_factory=lambda: AreaConfig(name="area-1"))
    processing_crs: str = "EPSG:3763"
    # Processos para o ajuste dos telhados e a deteção de candidatos (resultados iguais
    # aos de um só processo). None: núcleos − 1, até 8; 1: em série.
    workers: int | None = Field(default=None, ge=1, le=64)
    paths: PathsConfig = PathsConfig()
    sources: SourcesConfig = SourcesConfig()
    footprints: FootprintsConfig = FootprintsConfig()
    heights: HeightsConfig = HeightsConfig()
    roofs: RoofsConfig = RoofsConfig()
    appearance: AppearanceConfig = AppearanceConfig()
    export: ExportConfig = ExportConfig()
    topology: TopologyConfig = TopologyConfig()


LOCAL_CONFIG_NAME = "local.yaml"


def _merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """``override`` sobre ``base``, chave a chave (os dicionários juntam-se)."""
    out = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _merge(out[key], value)
        else:
            out[key] = value
    return out


def load_config(path: Path, *, local: bool = True) -> Config:
    """Lê ``path``; com ``local``, junta por cima o ``local.yaml`` da mesma pasta.

    O ``config.yaml`` (versionado) é geral. O que é de uma instalação (a área, o
    geoportal municipal autorizado, ...) vai para ``config/local.yaml``, ignorado pelo
    git, com só as chaves que mudam.
    """
    with path.open(encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}
    extra = path.with_name(LOCAL_CONFIG_NAME)
    if local and path.name != LOCAL_CONFIG_NAME and extra.exists():
        with extra.open(encoding="utf-8") as fh:
            raw = _merge(raw, yaml.safe_load(fh) or {})
    return Config.model_validate(raw)
