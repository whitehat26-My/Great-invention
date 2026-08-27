"""Liveness and readiness.

/health answers "is this process up"; /ready answers "can it actually do its
job", which means checking Postgres and Redis rather than returning 200
regardless. A readiness probe that cannot fail is not a readiness probe.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Response, status
from sqlalchemy import text

from restaurant_ai import __version__
from restaurant_ai.config import get_settings
from restaurant_ai.db.base import get_engine
from restaurant_ai.kernel.llm import describe_provider

router = APIRouter(tags=["health"])


@router.get("/health")
async def health() -> dict[str, Any]:
    settings = get_settings()
    return {
        "status": "ok",
        "version": __version__,
        "restaurant": settings.restaurant_name,
        "timezone": settings.timezone,
        "llm": describe_provider(),
    }


@router.get("/ready")
async def ready(response: Response) -> dict[str, Any]:
    checks: dict[str, Any] = {}

    try:
        with get_engine().connect() as conn:
            conn.execute(text("select 1"))
        checks["postgres"] = "ok"
    except Exception as exc:
        checks["postgres"] = f"error: {exc}"

    try:
        import redis

        redis.from_url(get_settings().redis_url, socket_connect_timeout=2).ping()
        checks["redis"] = "ok"
    except Exception as exc:
        checks["redis"] = f"error: {exc}"

    healthy = all(v == "ok" for v in checks.values())
    if not healthy:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return {"ready": healthy, "checks": checks}
