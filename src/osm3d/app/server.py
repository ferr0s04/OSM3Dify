"""API HTTP da aplicação de revisão (FastAPI) e frontend estático."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal, TypeVar

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from osm3d.app.service import NotFound, ReviewService
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


def create_app(cfg: Config, service: ReviewService | None = None) -> FastAPI:
    svc = service or ReviewService(cfg)
    app = FastAPI(title="osm3d — revisão", docs_url="/api/docs", openapi_url="/api/openapi.json")

    def key_of(osm_type: str, osm_id: int) -> str:
        if osm_type not in ("way", "relation"):
            raise HTTPException(404, "Tipo OSM inválido")
        return f"{osm_type}/{osm_id}"

    def call(fn: Callable[..., T], *args: Any) -> T:
        try:
            return fn(*args)
        except NotFound as exc:
            raise HTTPException(404, f"Edifício não encontrado: {exc}") from exc

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
            svc.save_review, key_of(osm_type, osm_id), body.status, body.note, body.overrides
        )

    @app.delete("/api/buildings/{osm_type}/{osm_id}/review")
    def reset_review(osm_type: str, osm_id: int) -> dict[str, Any]:
        return call(svc.reset_review, key_of(osm_type, osm_id))

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
