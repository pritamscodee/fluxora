from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api.v1.router import api_router
from app.core.config import settings
from app.core.errors import (
    FluxoraError,
    fluxora_error_handler,
    http_error_handler,
    validation_error_handler,
)
from app.core.db import engine, init_models
from app.core.telemetry import setup_telemetry

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
)
log = logging.getLogger("fluxora")

DESCRIPTION = """
Fluxora is an API-first video platform: direct uploads, FFmpeg transcoding to adaptive
HLS, signed playback, webhooks, analytics, and agentic workflow automation.

**Authentication**

* Dashboard routes (`/v1/auth`, `/v1/projects`) take a `Bearer` access token.
* Video API routes (`/v1/video/*`) take HTTP Basic auth with an API key id and secret.
* Delivery routes (`/v1/stream/*`) are public, or require `?token=` for signed playback.
"""


@asynccontextmanager
async def lifespan(app: FastAPI):
    Path(settings.media_scratch_dir).mkdir(parents=True, exist_ok=True)
    if settings.is_sqlite:
        # Postgres deployments migrate with Alembic; the SQLite dev path self-creates.
        await init_models()
    log.info(
        "fluxora api ready env=%s db=%s storage=%s",
        settings.environment,
        "sqlite" if settings.is_sqlite else "postgres",
        settings.s3_endpoint if settings.storage_configured else "local filesystem",
    )
    yield
    await engine.dispose()


app = FastAPI(
    title="Fluxora Video API",
    description=DESCRIPTION,
    version="0.1.0",
    lifespan=lifespan,
    docs_url="/docs",
    redoc_url="/redoc",
    openapi_url="/openapi.json",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["X-Total-Count", "X-Fluxora-Cache"],
)

# Instrumented at construction, not in the lifespan hook: FastAPIInstrumentor installs
# middleware, and Starlette refuses to accept new middleware once startup has begun.
setup_telemetry(app)

app.add_exception_handler(FluxoraError, fluxora_error_handler)
app.add_exception_handler(HTTPException, http_error_handler)
app.add_exception_handler(RequestValidationError, validation_error_handler)


@app.exception_handler(Exception)
async def unhandled_error_handler(request: Request, exc: Exception) -> JSONResponse:
    log.exception("unhandled error on %s %s", request.method, request.url.path)
    return JSONResponse(
        status_code=500,
        content={
            "error": {
                "type": "internal_error",
                "messages": ["An unexpected error occurred"],
            }
        },
    )


app.include_router(api_router)


@app.get("/health", tags=["meta"])
async def health():
    return {"status": "ok"}


@app.get("/healthz", tags=["meta"], include_in_schema=False)
async def healthz():
    """Readiness: fails if the database is unreachable, so a rolling deploy waits."""
    from sqlalchemy import text

    from app.core.db import SessionLocal

    async with SessionLocal() as session:
        await session.execute(text("SELECT 1"))
    return {"status": "ok", "database": "ok"}
