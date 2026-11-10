"""FastAPI application factory.

Run:  python -m voicecrm.cli serve      (or)      uvicorn voicecrm.api.main:app --port 8010
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .. import __version__
from ..config import REPO_ROOT, get_settings
from ..container import Container, build_container
from ..pipeline.service import PipelineError
from .routes import conversations, crm, demo, ingest, live, meta, metrics, policies, teams

logger = logging.getLogger(__name__)


def _spa_dir(configured: str) -> Path | None:
    for candidate in (Path(configured) if configured else None, REPO_ROOT / "frontend" / "dist", Path("/app/frontend/dist")):
        if candidate and (candidate / "index.html").exists():
            return candidate
    return None


def create_app(container: Container | None = None) -> FastAPI:
    settings = get_settings()
    container = container or build_container(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await container.startup()
        yield
        await container.shutdown()

    app = FastAPI(title="VoiceCRM", version=__version__, lifespan=lifespan,
                  description="Conversation-to-CRM hub: any call or meeting -> governed, CRM-ready output.")
    app.state.container = container
    app.add_middleware(CORSMiddleware, allow_origins=[o.strip() for o in container.settings.cors_origins.split(",") if o.strip()],
                       allow_methods=["*"], allow_headers=["*"], expose_headers=["X-Transcript-Source"])

    @app.exception_handler(PipelineError)
    async def _pipeline_error(_: Request, exc: PipelineError) -> JSONResponse:
        return JSONResponse(status_code=exc.status, content={"detail": str(exc)})

    for router in (meta.router, ingest.router, conversations.router, policies.router, crm.router, metrics.router,
                   teams.router, live.router, demo.router):
        app.include_router(router, prefix="/api/v1")

    spa = _spa_dir(container.settings.spa_dir)
    if spa is not None:
        index = spa / "index.html"
        app.mount("/assets", StaticFiles(directory=spa / "assets"), name="assets") if (spa / "assets").exists() else None

        @app.get("/{path:path}", include_in_schema=False)
        async def _spa(path: str) -> FileResponse:
            target = (spa / path).resolve()
            if path and target.is_file() and spa.resolve() in target.parents:
                return FileResponse(target)
            return FileResponse(index)

        logger.info("serving SPA from %s", spa)
    return app


_app: FastAPI | None = None


def __getattr__(name: str) -> Any:
    """Lazy module attribute so `uvicorn voicecrm.api.main:app` works without building the app at import time."""
    global _app
    if name == "app":
        if _app is None:
            _app = create_app()
        return _app
    raise AttributeError(name)
