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
from sqlalchemy import func, select

from restaurant_ai import clock
from restaurant_ai.config import get_settings
from restaurant_ai.db.base import session_scope
from restaurant_ai.db.models import (
    AgentRun,
    AgentRunStatus,
    ApprovalRequest,
    DailyReport,
)
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

        latest = _latest_runs(session, today)

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


def _latest_runs(session, today: date) -> dict[str, AgentRun]:
    """Last run of the day per agent — the day's final word wins."""
    latest: dict[str, AgentRun] = {}
    for row in session.execute(
        select(AgentRun).where(AgentRun.business_date == today).order_by(AgentRun.started_at)
    ).scalars():
        latest[row.agent_name] = row
    return latest


_MAP_PAGE = Path(__file__).with_name("dashboard_map.html")

_DEPARTMENT_LABELS = {
    "front_of_house": "Front of House",
    "kitchen": "Kitchen",
    "supply": "Supply Chain",
    "marketing": "Marketing",
    "workforce": "Workforce",
    "finance": "Finance",
}


@router.get("/map", response_class=HTMLResponse)
async def map_page(request: Request) -> HTMLResponse:
    _require_key(request)
    return HTMLResponse(_MAP_PAGE.read_text(encoding="utf-8"))


def _agent_history(session: Any, names: list[str]) -> dict[str, dict[str, Any]]:
    """How each agent has actually been behaving, not how it is configured.

    The map described the system as declared — tools, schedule, the brief it is
    given — and every one of those is true on a machine where nothing has run
    for a week. What it could not answer is the question someone opens it to
    ask: is this thing working?

    A week of runs is the window. Long enough that a nightly agent has seven
    points to judge, short enough that last month's fixed problem does not
    still colour it.
    """
    from datetime import timedelta

    from restaurant_ai.db.models import ApprovalStatus

    since = clock.utcnow() - timedelta(days=7)
    history: dict[str, dict[str, Any]] = {
        name: {"recent": [], "runs_7d": 0, "failed_7d": 0, "tokens_7d": 0, "pending": 0}
        for name in names
    }

    rows = session.execute(
        select(AgentRun)
        .where(AgentRun.agent_name.in_(names), AgentRun.started_at >= since)
        .order_by(AgentRun.started_at.desc())
    ).scalars()
    for row in rows:
        entry = history.get(row.agent_name)
        if entry is None:
            continue
        entry["runs_7d"] += 1
        entry["tokens_7d"] += row.tokens_used or 0
        if row.status == AgentRunStatus.FAILED:
            entry["failed_7d"] += 1
        # The ten most recent, since that is what a strip of dots can show.
        if len(entry["recent"]) < 10:
            took = (
                (row.finished_at - row.started_at).total_seconds()
                if row.finished_at and row.started_at
                else None
            )
            entry["recent"].append(
                {
                    "status": row.status.value,
                    "at": row.started_at.isoformat() if row.started_at else None,
                    "seconds": round(took, 1) if took is not None else None,
                    "summary": (row.summary or "")[:160],
                }
            )

    pending = session.execute(
        select(ApprovalRequest.agent_name, func.count())
        .where(
            ApprovalRequest.agent_name.in_(names),
            ApprovalRequest.status == ApprovalStatus.PENDING,
        )
        .group_by(ApprovalRequest.agent_name)
    )
    for name, count in pending:
        if name in history:
            history[name]["pending"] = int(count)

    for entry in history.values():
        timed = [r["seconds"] for r in entry["recent"] if r["seconds"] is not None]
        entry["typical_seconds"] = round(sorted(timed)[len(timed) // 2], 1) if timed else None
    return history


def _next_run_seconds(schedule: Any) -> int | None:
    """Seconds until the next firing, for anything that has to compare them.

    The words are for reading and cannot be ordered: "in 3 days" sorts before
    "in 4 hours" on any property of the strings themselves, so a header that
    reports the soonest firing needs the number the words were made from.
    """
    if schedule is None:
        return None
    try:
        return max(0, int(schedule.remaining_estimate(clock.now()).total_seconds()))
    except Exception:
        return None


def _next_run(schedule: Any) -> str | None:
    """When this fires next, in plain words.

    "Hourly review sweep" is the rule; "in 14 minutes" is the thing someone
    watching the map wants to know, and only one of them tells you whether the
    quiet you are looking at is expected.
    """
    if schedule is None:
        return None
    try:
        remaining = schedule.remaining_estimate(clock.now())
    except Exception:
        return None
    seconds = int(remaining.total_seconds())
    if seconds <= 0:
        return "due now"
    if seconds < 90:
        return f"in {seconds} seconds"
    if seconds < 5400:
        return f"in {seconds // 60} minutes"
    if seconds < 172800:
        return f"in {seconds // 3600} hours"
    return f"in {seconds // 86400} days"


@router.get("/map/data")
async def map_data(request: Request) -> dict[str, Any]:
    """The system, as the registry declares it.

    Everything here is read off the AgentSpecs and the beat schedule — the same
    objects the runtime executes — so the map cannot describe an agent the
    system does not have, or a tool an agent cannot call.
    """
    _require_key(request)

    from restaurant_ai.kernel.registry import all_agents, departments
    from restaurant_ai.worker.celery_app import SCHEDULE

    with session_scope() as session:
        latest = _latest_runs(session, clock.today())

    names = sorted(all_agents())
    with session_scope() as session:
        history = _agent_history(session, names)

    grouped: dict[str, list[dict[str, Any]]] = {name: [] for name in departments()}
    for name, spec in sorted(all_agents().items()):
        run = latest.get(name)
        cron, reason = SCHEDULE.get(name, (None, "Runs when events arrive, not on a clock."))
        seen = history.get(name, {})
        grouped[spec.department].append(
            {
                "name": name,
                "person": spec.person or name,
                "title": spec.title,
                "description": spec.description,
                "model_tier": spec.model_tier,
                "schedule": reason,
                "brief": spec.system_prompt,
                "status": run.status.value if run else "idle",
                "last_summary": (run.summary or "")[:280] if run else "",
                "last_run_at": run.started_at.isoformat() if run and run.started_at else None,
                "next_run": _next_run(cron),
                "next_run_seconds": _next_run_seconds(cron),
                "recent": seen.get("recent", []),
                "runs_7d": seen.get("runs_7d", 0),
                "failed_7d": seen.get("failed_7d", 0),
                "tokens_7d": seen.get("tokens_7d", 0),
                "typical_seconds": seen.get("typical_seconds"),
                "pending_approvals": seen.get("pending", 0),
                "tools": [
                    {
                        "name": tool.name,
                        "description": tool.description,
                        "gated": tool.requires_approval,
                    }
                    for tool in spec.tools
                ],
            }
        )

    return {
        "restaurant": get_settings().restaurant_name,
        "departments": [
            {"name": name, "label": _DEPARTMENT_LABELS.get(name, name), "agents": agents}
            for name, agents in grouped.items()
        ],
    }


def _iso(value: date) -> str:
    return value.isoformat()
