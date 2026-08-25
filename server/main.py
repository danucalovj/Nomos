"""FastAPI application entrypoint. Run via ./start.sh (uvicorn, single worker)."""
from __future__ import annotations

import time
from contextlib import asynccontextmanager
from collections.abc import AsyncIterator

from fastapi import FastAPI, Request, Response
from fastapi.staticfiles import StaticFiles
from pathlib import Path

from . import db
from .config import get_settings
from .errors import install_error_handlers
from .logging_setup import kv, setup_logging
from .migrate import run_migrations
from . import fsmonitor
from .routers import (
    agents,
    attachments,
    audit,
    board,
    channels,
    documents,
    messages,
    meta,
    projects,
    search,
    setup,
    stream,
    tickets,
)

WEBUI_DIR = Path(__file__).resolve().parent.parent / "webui"


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    logger = setup_logging()
    settings = get_settings()
    settings.attachments_dir.mkdir(parents=True, exist_ok=True)
    applied = run_migrations()
    if applied:
        logger.info("migrations applied %s", kv(files=",".join(applied)))
    logger.info(
        "startup %s",
        kv(host=settings.host, port=settings.port, data_dir=settings.data_dir),
    )
    fsmonitor.start_all_watches()
    yield
    fsmonitor.stop_all_watches()
    db.checkpoint()
    db.close_conn()
    logger.info("shutdown complete (WAL checkpointed)")


app = FastAPI(
    title="Nomos",
    version="1.0.0",
    lifespan=lifespan,
    docs_url="/api/docs",
    openapi_url="/api/openapi.json",
)
install_error_handlers(app)


@app.middleware("http")
async def request_logging(request: Request, call_next) -> Response:
    start = time.monotonic()
    response = await call_next(request)
    if request.url.path.startswith("/api"):
        setup_logging().info(
            "request %s",
            kv(
                method=request.method,
                path=request.url.path,
                status=response.status_code,
                ms=round((time.monotonic() - start) * 1000, 1),
            ),
        )
    return response


for router in (
    setup.router,
    meta.router,
    audit.router,
    projects.router,
    agents.router,
    channels.router,
    messages.router,
    stream.router,
    tickets.router,
    board.router,
    documents.router,
    search.router,
    attachments.router,
):
    app.include_router(router, prefix="/api")

app.mount("/", StaticFiles(directory=WEBUI_DIR, html=True), name="webui")
