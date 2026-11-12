"""API HTTP da aplicação de revisão (FastAPI) e frontend estático."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal, TypeVar

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, ValidationError

from osm3d.acquisition.osm import OsmLookupError
from osm3d.app.context import AppContext, RelationLookup
from osm3d.app.downloads import DownloadManager
from osm3d.app.ortho import OrthoTiles
from osm3d.app.processing import PipelineJob
from osm3d.app.service import LayoutIn, NotFound, PartsIn, ReviewService
from osm3d.config import Config
from osm3d.domain.models import ReviewStatus
from osm3d.pipeline import Stage
from osm3d.review import Overrides

STATIC_DIR = Path(__file__).parent / "static"
T = TypeVar("T")


class ArchiveIn(BaseModel):
    status: Literal["accepted", "rejected"]
    archived: bool = True


class ReviewIn(BaseModel):
    status: ReviewStatus
    note: str | None = Field(default=None, max_length=2000)
    overrides: Overrides = Field(default_factory=Overrides)
    parts: PartsIn | None = None  # divisão em partes (None = edifício único)


class SplitIn(BaseModel):
    layout: LayoutIn
    part: int
    a: tuple[float, float]
    b: tuple[float, float]


class MergeIn(BaseModel):
    layout: LayoutIn
    part: int


class TilesIn(BaseModel):
    tiles: list[str] = Field(min_length=1, max_length=5000)
    products: list[str] = Field(default_factory=lambda: ["lidar", "dtm"], min_length=1)


class DeleteIn(TilesIn):
    products: list[str] = Field(
        default_factory=lambda: ["lidar", "dtm", "dsm", "orthophoto"], min_length=1
    )
    include_cache: bool = True


class AreaIn(BaseModel):
    label: str | None = Field(default=None, max_length=200)
    # A área é este conjunto de quadrículas: LAZ/MDT e edifícios OSM vêm das mesmas.
    tiles: list[str] = Field(min_length=1, max_length=5000)
    activate: bool = False


class ProcessIn(BaseModel):
    candidates: bool = False  # também a fase "candidates" (edifícios em falta no OSM)
    # Por omissão só corre o que falta ou está desatualizado. Com uma fase, refaz essa e
    # as seguintes (fetch: volta a pedir os edifícios ao Overpass).
    redo_from: Stage | None = None


def create_app(
    cfg: Config,
    service: ReviewService | None = None,
    downloads: DownloadManager | None = None,
    pipeline: PipelineJob | None = None,
    relation_lookup: RelationLookup | None = None,
) -> FastAPI:
    """``cfg`` é a configuração do config.yaml; a área processada é a área ativa.

    Sem resultados do pipeline, a aplicação arranca na mesma: o separador "Dados"
    serve para escolher a área, descarregar as quadrículas e processá-la.
    """
    ctx = AppContext(cfg, service, downloads, pipeline, relation_lookup)
    data = ctx.data
    ortho = OrthoTiles(cfg.sources.orthophoto_dir)
    app = FastAPI(title="osm3d — revisão", docs_url="/api/docs", openapi_url="/api/openapi.json")
    app.state.ctx = ctx
    app.state.data = data

    @app.middleware("http")
    async def no_stale_frontend(request: Request, call_next: Any) -> Response:
        # Sem isto, o browser pode misturar módulos JS antigos (em cache) com novos depois
        # de uma atualização. "no-cache" obriga a revalidar (ETag): só descarrega se mudou.
        response: Response = await call_next(request)
        if request.url.path == "/" or request.url.path.startswith("/static/"):
            response.headers["Cache-Control"] = "no-cache"
        return response

    def rs() -> ReviewService:
        svc = ctx.svc
        if svc is None:
            raise HTTPException(503, ctx.review_error or "Sem resultados para rever")
        return svc

    def key_of(osm_type: str, osm_id: int) -> str:
        if osm_type not in ("way", "relation", "new"):  # new: em falta no OSM (candidato)
            raise HTTPException(404, "Tipo OSM inválido")
        return f"{osm_type}/{osm_id}"

    def call(fn: Callable[..., T], *args: Any) -> T:
        try:
            return fn(*args)
        except NotFound as exc:
            raise HTTPException(404, f"Edifício não encontrado: {exc}") from exc
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.get("/api/summary")
    def summary() -> dict[str, Any]:
        return rs().summary()

    @app.get("/api/buildings")
    def buildings() -> list[dict[str, Any]]:
        return rs().rows()

    @app.get("/api/buildings.geojson")
    def buildings_geojson() -> dict[str, Any]:
        return rs().geojson()

    @app.get("/api/buildings/{osm_type}/{osm_id}")
    def detail(osm_type: str, osm_id: int) -> dict[str, Any]:
        return call(rs().detail, key_of(osm_type, osm_id))

    @app.get("/api/buildings/{osm_type}/{osm_id}/mesh")
    def mesh(osm_type: str, osm_id: int) -> dict[str, Any]:
        return call(rs().mesh, key_of(osm_type, osm_id))

    @app.get("/api/buildings/{osm_type}/{osm_id}/points")
    def points(osm_type: str, osm_id: int) -> dict[str, Any]:
        return call(rs().points_for, key_of(osm_type, osm_id))

    @app.post("/api/buildings/{osm_type}/{osm_id}/preview")
    def preview(osm_type: str, osm_id: int, overrides: Overrides) -> dict[str, Any]:
        return call(rs().preview, key_of(osm_type, osm_id), overrides)

    @app.put("/api/buildings/{osm_type}/{osm_id}/review")
    def save_review(osm_type: str, osm_id: int, body: ReviewIn) -> dict[str, Any]:
        return call(
            rs().save_review,
            key_of(osm_type, osm_id),
            body.status,
            body.note,
            body.overrides,
            body.parts,
        )

    # --- partes (building:part) -------------------------------------------------

    @app.get("/api/buildings/{osm_type}/{osm_id}/parts")
    def parts_view(osm_type: str, osm_id: int) -> dict[str, Any]:
        return call(rs().parts_view, key_of(osm_type, osm_id))

    @app.post("/api/buildings/{osm_type}/{osm_id}/parts/start")
    def parts_start(osm_type: str, osm_id: int) -> dict[str, Any]:
        return call(rs().parts_start, key_of(osm_type, osm_id))

    @app.post("/api/buildings/{osm_type}/{osm_id}/parts/preview")
    def parts_preview(osm_type: str, osm_id: int, body: PartsIn) -> dict[str, Any]:
        return call(rs().parts_preview, key_of(osm_type, osm_id), body.layout, body.overrides)

    @app.post("/api/buildings/{osm_type}/{osm_id}/parts/split")
    def parts_split(osm_type: str, osm_id: int, body: SplitIn) -> dict[str, Any]:
        return call(
            rs().parts_split, key_of(osm_type, osm_id), body.layout, body.part, body.a, body.b
        )

    @app.post("/api/buildings/{osm_type}/{osm_id}/parts/merge")
    def parts_merge(osm_type: str, osm_id: int, body: MergeIn) -> dict[str, Any]:
        return call(rs().parts_merge, key_of(osm_type, osm_id), body.layout, body.part)

    @app.delete("/api/buildings/{osm_type}/{osm_id}/review")
    def reset_review(osm_type: str, osm_id: int) -> dict[str, Any]:
        return call(rs().reset_review, key_of(osm_type, osm_id))

    @app.get("/api/scene")
    def scene() -> dict[str, Any]:
        """Telhados 3D (forma e cores inferidas) de todos os edifícios, para o mapa."""
        return rs().scene()

    @app.get("/api/buildings/{osm_type}/{osm_id}/scene")
    def scene_one(osm_type: str, osm_id: int) -> dict[str, Any] | None:
        return call(rs().scene_one, key_of(osm_type, osm_id))

    @app.post("/api/reviews/archive")
    def archive(body: ArchiveIn) -> dict[str, Any]:
        """Arquiva (ou desarquiva) todos os aceites ou todos os rejeitados."""
        return rs().archive(body.status, body.archived)

    @app.post("/api/export")
    def export() -> dict[str, Any]:
        return rs().export()

    # --- dados raw (quadrículas DGT) -------------------------------------------

    @app.get("/api/data/status")
    def data_status() -> dict[str, Any]:
        """Grelha, área configurada, ficheiros locais por quadrícula e downloads."""
        return data.status()

    @app.get("/api/data/tiles")
    def data_tiles(bbox: str | None = None, names: str | None = None) -> dict[str, Any]:
        """Quadrículas (GeoJSON) numa bbox WGS84 ``w,s,e,n`` ou por nome (``a,b,…``)."""
        try:
            if names is not None:
                return data.tiles([n for n in names.split(",") if n])
            if bbox is None:
                raise ValueError("Indicar bbox=w,s,e,n ou names=…")
            w, s, e, n = (float(v) for v in bbox.split(","))
            return data.tiles_in_bbox((w, s, e, n))
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.get("/api/data/downloads")
    def downloads_state() -> dict[str, Any]:
        return data.downloads.state()

    @app.post("/api/data/downloads")
    def downloads_submit(body: TilesIn) -> dict[str, Any]:
        """Põe em fila o download (em segundo plano) dos produtos em falta."""
        return call(data.download, body.tiles, body.products)

    @app.delete("/api/data/downloads")
    def downloads_cancel() -> dict[str, Any]:
        return data.downloads.cancel()

    @app.post("/api/data/delete")
    def data_delete(body: DeleteIn) -> dict[str, Any]:
        """Apaga os ficheiros raw (e as caches de pontos) das quadrículas indicadas."""
        try:
            return call(data.delete, body.tiles, body.products, body.include_cache)
        except RuntimeError as exc:
            raise HTTPException(409, str(exc)) from exc

    # --- ortofotos (fundo do mapa) ----------------------------------------------

    @app.get("/api/ortho/info")
    def ortho_info() -> dict[str, Any]:
        """Ortofotos descarregadas (para o fundo do mapa): ano e extensão WGS84."""
        files = ortho.files()
        return {
            "available": bool(files),
            "files": [
                {"name": f.path.name, "year": f.year, "bbox": list(f.bounds_wgs84)} for f in files
            ],
        }

    @app.get("/api/ortho/{z}/{x}/{y}.png")
    def ortho_tile(z: int, x: int, y: int) -> Response:
        """Tile XYZ das ortofotos locais (204 fora delas)."""
        try:
            png = ortho.tile(z, x, y)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        if png is None:
            return Response(status_code=204)
        return Response(png, media_type="image/png", headers={"Cache-Control": "max-age=3600"})

    # --- áreas e processamento ---------------------------------------------------

    def guarded(fn: Callable[..., T], *args: Any, **kwargs: Any) -> T:
        try:
            return fn(*args, **kwargs)
        except KeyError as exc:
            raise HTTPException(404, f"Área não encontrada: {exc}") from exc
        except (FileExistsError, RuntimeError) as exc:
            raise HTTPException(409, str(exc)) from exc
        except (ValueError, ValidationError) as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.get("/api/areas")
    def areas() -> dict[str, Any]:
        """Áreas registadas, a ativa, cobertura LiDAR local e estado do processamento."""
        return ctx.areas()

    @app.post("/api/areas")
    def area_create(body: AreaIn) -> dict[str, Any]:
        """Área nova; o id (``area-<n>``) é gerado, ``label`` é só o nome a mostrar."""
        return guarded(
            ctx.save_area, None, body.label, body.tiles, create=True, activate=body.activate
        )

    @app.put("/api/areas/{name}")
    def area_update(name: str, body: AreaIn) -> dict[str, Any]:
        return guarded(
            ctx.save_area, name, body.label, body.tiles, create=False, activate=body.activate
        )

    @app.post("/api/areas/{name}/activate")
    def area_activate(name: str) -> dict[str, Any]:
        return guarded(ctx.activate, name)

    @app.delete("/api/areas/{name}")
    def area_delete(name: str) -> dict[str, Any]:
        return guarded(ctx.delete_area, name)

    @app.get("/api/osm/relation/{relation_id}")
    def osm_relation(relation_id: int) -> dict[str, Any]:
        """Nome e quadrículas de um limite OSM (relação), para escolher as de uma área."""
        try:
            return ctx.lookup_relation(relation_id)
        except OsmLookupError as exc:
            raise HTTPException(502, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.get("/api/processing")
    def processing_state() -> dict[str, Any]:
        return ctx.pipeline.state()

    @app.post("/api/processing")
    def processing_start(body: ProcessIn) -> dict[str, Any]:
        """Corre o pipeline (fetch → qa) da área ativa em segundo plano."""
        return guarded(ctx.process, candidates=body.candidates, redo_from=body.redo_from)

    @app.delete("/api/processing")
    def processing_cancel() -> dict[str, Any]:
        return ctx.pipeline.cancel()

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    return app
