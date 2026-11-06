"""FastAPI app. Endpoint specs are coming later; they should call the functions in
`app.messaging.service` inside a request-scoped session and commit once at the end."""

import logging

import redis
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import text

from app.config import get_settings
from app.db import get_engine

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
def healthz() -> JSONResponse:
    checks: dict[str, str] = {}
    try:
        with get_engine().connect() as conn:
            conn.execute(text("SELECT 1"))
        checks["database"] = "ok"
    except Exception as exc:
        log.warning("healthz: database check failed: %s", exc)
        checks["database"] = "error"
    try:
        redis.Redis.from_url(settings.redis_url, socket_timeout=2, socket_connect_timeout=2).ping()
        checks["redis"] = "ok"
    except Exception as exc:
        log.warning("healthz: redis check failed: %s", exc)
        checks["redis"] = "error"
    healthy = all(v == "ok" for v in checks.values())
    return JSONResponse(checks, status_code=200 if healthy else 503)
