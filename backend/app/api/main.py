"""FastAPI app. Endpoints take an `AsyncSession` from `Depends(get_db)`, call the functions in
`app.messaging.async_service`, and commit once at the end."""

import logging

import redis.asyncio as redis
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import text

from app.config import get_settings
from app.db import get_async_engine

log = logging.getLogger(__name__)
settings = get_settings()

app = FastAPI(title="PM Dashboard API")
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.frontend_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/healthz")
async def healthz() -> JSONResponse:
    checks: dict[str, str] = {}
    try:
        async with get_async_engine().connect() as conn:
            await conn.execute(text("SELECT 1"))
        checks["database"] = "ok"
    except Exception as exc:
        log.warning("healthz: database check failed: %s", exc)
        checks["database"] = "error"
    try:
        async with redis.Redis.from_url(settings.redis_url, socket_timeout=2, socket_connect_timeout=2) as r:
            await r.ping()
        checks["redis"] = "ok"
    except Exception as exc:
        log.warning("healthz: redis check failed: %s", exc)
        checks["redis"] = "error"
    healthy = all(v == "ok" for v in checks.values())
    return JSONResponse(checks, status_code=200 if healthy else 503)
