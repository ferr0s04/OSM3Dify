"""API HTTP da aplicação de revisão (FastAPI) e frontend estático."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal, TypeVar

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from osm3d.app.service import LayoutIn, NotFound, PartsIn, ReviewService
from osm3d.config import Config
from osm3d.domain.models import ReviewStatus
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


def create_app(cfg: Config, service: ReviewService | None = None) -> FastAPI:
    svc = service or ReviewService(cfg)
    app = FastAPI(title="osm3d — revisão", docs_url="/api/docs", openapi_url="/api/openapi.json")

    @app.middleware("http")
    async def no_stale_frontend(request: Request, call_next: Any) -> Response:
        # Sem isto, o browser pode misturar módulos JS antigos (em cache) com novos depois
        # de uma atualização. "no-cache" obriga a revalidar (ETag): só descarrega se mudou.
        response: Response = await call_next(request)
        if request.url.path == "/" or request.url.path.startswith("/static/"):
            response.headers["Cache-Control"] = "no-cache"
        return response

    def key_of(osm_type: str, osm_id: int) -> str:
        if osm_type not in ("way", "relation"):
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
        return svc.summary()

    @app.get("/api/buildings")
    def buildings() -> list[dict[str, Any]]:
        return svc.rows()

    @app.get("/api/buildings.geojson")
    def buildings_geojson() -> dict[str, Any]:
        return svc.geojson()

    @app.get("/api/buildings/{osm_type}/{osm_id}")
    def detail(osm_type: str, osm_id: int) -> dict[str, Any]:
        return call(svc.detail, key_of(osm_type, osm_id))

    @app.get("/api/buildings/{osm_type}/{osm_id}/mesh")
    def mesh(osm_type: str, osm_id: int) -> dict[str, Any]:
        return call(svc.mesh, key_of(osm_type, osm_id))

    @app.get("/api/buildings/{osm_type}/{osm_id}/points")
    def points(osm_type: str, osm_id: int) -> dict[str, Any]:
        return call(svc.points_for, key_of(osm_type, osm_id))

    @app.post("/api/buildings/{osm_type}/{osm_id}/preview")
    def preview(osm_type: str, osm_id: int, overrides: Overrides) -> dict[str, Any]:
        return call(svc.preview, key_of(osm_type, osm_id), overrides)

    @app.put("/api/buildings/{osm_type}/{osm_id}/review")
    def save_review(osm_type: str, osm_id: int, body: ReviewIn) -> dict[str, Any]:
        return call(
            svc.save_review,
            key_of(osm_type, osm_id),
            body.status,
            body.note,
            body.overrides,
            body.parts,
        )

    # --- partes (building:part) -------------------------------------------------

    @app.get("/api/buildings/{osm_type}/{osm_id}/parts")
    def parts_view(osm_type: str, osm_id: int) -> dict[str, Any]:
        return call(svc.parts_view, key_of(osm_type, osm_id))

    @app.post("/api/buildings/{osm_type}/{osm_id}/parts/start")
    def parts_start(osm_type: str, osm_id: int) -> dict[str, Any]:
        return call(svc.parts_start, key_of(osm_type, osm_id))

    @app.post("/api/buildings/{osm_type}/{osm_id}/parts/preview")
    def parts_preview(osm_type: str, osm_id: int, body: PartsIn) -> dict[str, Any]:
        return call(svc.parts_preview, key_of(osm_type, osm_id), body.layout, body.overrides)

    @app.post("/api/buildings/{osm_type}/{osm_id}/parts/split")
    def parts_split(osm_type: str, osm_id: int, body: SplitIn) -> dict[str, Any]:
        return call(
            svc.parts_split, key_of(osm_type, osm_id), body.layout, body.part, body.a, body.b
        )

    @app.post("/api/buildings/{osm_type}/{osm_id}/parts/merge")
    def parts_merge(osm_type: str, osm_id: int, body: MergeIn) -> dict[str, Any]:
        return call(svc.parts_merge, key_of(osm_type, osm_id), body.layout, body.part)

    @app.delete("/api/buildings/{osm_type}/{osm_id}/review")
    def reset_review(osm_type: str, osm_id: int) -> dict[str, Any]:
        return call(svc.reset_review, key_of(osm_type, osm_id))

    @app.get("/api/scene")
    def scene() -> dict[str, Any]:
        """Telhados 3D (forma e cores inferidas) de todos os edifícios, para o mapa."""
        return svc.scene()

    @app.get("/api/buildings/{osm_type}/{osm_id}/scene")
    def scene_one(osm_type: str, osm_id: int) -> dict[str, Any] | None:
        return call(svc.scene_one, key_of(osm_type, osm_id))

    @app.post("/api/reviews/archive")
    def archive(body: ArchiveIn) -> dict[str, Any]:
        """Arquiva (ou desarquiva) todos os aceites ou todos os rejeitados."""
        return svc.archive(body.status, body.archived)

    @app.post("/api/export")
    def export() -> dict[str, Any]:
        return svc.export()

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    return app
