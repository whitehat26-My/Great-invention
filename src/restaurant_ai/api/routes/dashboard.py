"""The live dashboard.

The Telegram brief is the daily pulse; this is the standing picture — the same
numbers, on a screen, refreshing while service runs. It is one self-contained
HTML page (no CDN, no build step: it has to work on a laptop in a restaurant
with flaky wifi) fed by one JSON endpoint.

Both are guarded by the same key as the rest of the approval surface, and fail
closed the same way: no key configured means the dashboard refuses to serve,
not that it serves anyone. The page itself carries no data — everything arrives
via the authenticated fetch — so a leaked URL without the key shows an empty
shell.
"""

from __future__ import annotations

import hmac
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import HTMLResponse
from sqlalchemy import select

from restaurant_ai import clock
from restaurant_ai.config import get_settings
from restaurant_ai.db.base import session_scope
from restaurant_ai.db.models import AgentRun, DailyReport
from restaurant_ai.logging_setup import get_logger

log = get_logger(__name__)
router = APIRouter(prefix="/dashboard", tags=["dashboard"])

_PAGE = Path(__file__).with_name("dashboard.html")


def _require_key(request: Request) -> None:
    """Header or ?key= — a browser address bar cannot set a header.

    Same posture as the rest of the approval surface: unset refuses.
    """
    configured = get_settings().approval_api_key
    if not configured:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="APPROVAL_API_KEY is not configured, so the dashboard is closed.",
        )
    supplied = request.headers.get("x-api-key") or request.query_params.get("key") or ""
    if not hmac.compare_digest(supplied, configured):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Bad or missing key.")


@router.get("", response_class=HTMLResponse)
async def dashboard_page(request: Request) -> HTMLResponse:
    _require_key(request)
    return HTMLResponse(_PAGE.read_text(encoding="utf-8"))


@router.get("/data")
async def dashboard_data(request: Request) -> dict[str, Any]:
    _require_key(request)

    from restaurant_ai.brief import build_brief
    from restaurant_ai.kernel.registry import all_agents

    settings = get_settings()
    today = clock.today()

    with session_scope() as session:
        brief = build_brief(session)

        history = [
            {
                "date": report.business_date.isoformat(),
                "revenue": float(report.net_revenue),
                "covers": report.covers,
                "prime_pct": float(report.prime_cost_pct) * 100,
                "labour_pct": float(report.labour_pct) * 100,
                "margin_pct": float(report.operating_margin_pct) * 100,
            }
            for report in session.execute(
                select(DailyReport)
                .where(DailyReport.business_date >= today - timedelta(days=14))
                .order_by(DailyReport.business_date)
            ).scalars()
        ]

        latest: dict[str, AgentRun] = {}
        for row in session.execute(
            select(AgentRun).where(AgentRun.business_date == today).order_by(AgentRun.started_at)
        ).scalars():
            latest[row.agent_name] = row

        agents = []
        for name, spec in sorted(all_agents().items(), key=lambda kv: kv[1].department):
            run: AgentRun | None = latest.get(name)
            agents.append(
                {
                    "person": spec.person or name,
                    "title": spec.title,
                    "department": spec.department,
                    "status": run.status.value if run else "idle",
                    "summary": (run.summary or "")[:200] if run else "has not run today",
                }
            )

    return {
        "restaurant": settings.restaurant_name,
        "generated_at": clock.utcnow().isoformat(),
        "business_date": _iso(brief.business_date),
        "sections": brief.sections,
        "needs_you": brief.needs_you,
        "failures": brief.failures,
        "history": history,
        "agents": agents,
    }


def _iso(value: date) -> str:
    return value.isoformat()
