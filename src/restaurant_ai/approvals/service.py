"""The approval service.

One path, three doors. Slack buttons, Telegram callbacks and the REST endpoint
all arrive here, and this is the only place a parked graph is resumed.

The mechanism rests on the thread id. When an agent interrupts, LangGraph
checkpoints the graph to Postgres and the process unwinds. An ``approval_request``
row records the thread id alongside what the human needs to see. Later — in
another process, possibly after a deploy — resolving that row resumes the graph
from exactly where it stopped and lets it run on to commit.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import select

from restaurant_ai import clock
from restaurant_ai.config import get_settings
from restaurant_ai.db.base import session_scope
from restaurant_ai.db.models import AgentRun, AgentRunStatus, ApprovalRequest, ApprovalStatus
from restaurant_ai.events import Event, Topic, publish
from restaurant_ai.logging_setup import get_logger

log = get_logger(__name__)

# How long a request stays actionable. A purchase order approved three days late
# orders against demand that has moved on.
DEFAULT_TTL_HOURS = 24


def record_request(
    run_id: str,
    thread_id: str,
    agent_name: str,
    title: str,
    detail: str,
    payload: dict[str, Any],
    value: Decimal = Decimal("0"),
    ttl_hours: int = DEFAULT_TTL_HOURS,
    session=None,
) -> ApprovalRequest:
    """Persist a pending approval so it can be resolved from anywhere later."""
    with session_scope(session) as db:
        request = ApprovalRequest(
            run_id=run_id,
            thread_id=thread_id,
            agent_name=agent_name,
            title=title[:200],
            detail=detail,
            payload=payload,
            value=value,
            status=ApprovalStatus.PENDING,
            channel=get_settings().approval_channel,
            requested_at=clock.utcnow(),
            expires_at=clock.utcnow() + timedelta(hours=ttl_hours),
        )
        db.add(request)
        db.flush()
        publish(
            Event(
                Topic.APPROVAL_REQUESTED,
                {"approval_id": request.id, "agent": agent_name, "value": str(value)},
                source_run_id=run_id,
            ),
            session=db,
        )
        return request


def dispatch(approval_id: str) -> dict[str, Any]:
    """Send the approval card to whichever channel is configured."""
    settings = get_settings()
    with session_scope() as session:
        request = session.get(ApprovalRequest, approval_id)
        if request is None:
            raise KeyError(f"No approval request {approval_id}.")
        snapshot = {
            "id": request.id,
            "agent_name": request.agent_name,
            "title": request.title,
            "detail": request.detail,
            "value": request.value,
        }

    if settings.approval_channel == "slack":
        from restaurant_ai.approvals.slack import send_approval_card

        ref = send_approval_card(snapshot)
    elif settings.approval_channel == "telegram":
        from restaurant_ai.approvals.telegram import send_approval_card

        ref = send_approval_card(snapshot)
    else:
        # No channel configured: the request still exists and is resolvable via
        # the CLI or the API, so the work is not lost, just not pushed anywhere.
        log.info(
            "approval pending with no channel configured",
            approval_id=approval_id,
            title=snapshot["title"],
        )
        ref = None

    if ref:
        with session_scope() as session:
            request = session.get(ApprovalRequest, approval_id)
            if request is not None:
                request.channel_message_ref = ref

    return {"approval_id": approval_id, "channel": settings.approval_channel, "ref": ref}


def dispatch_pending_for_run(run_id: str) -> list[str]:
    """Send cards for every pending approval belonging to a run."""
    with session_scope() as session:
        ids = [
            r.id
            for r in session.execute(
                select(ApprovalRequest).where(
                    ApprovalRequest.run_id == run_id,
                    ApprovalRequest.status == ApprovalStatus.PENDING,
                )
            ).scalars()
        ]
    for approval_id in ids:
        dispatch(approval_id)
    return ids


def list_pending(limit: int = 50) -> list[dict[str, Any]]:
    with session_scope() as session:
        rows = list(
            session.execute(
                select(ApprovalRequest)
                .where(ApprovalRequest.status == ApprovalStatus.PENDING)
                .order_by(ApprovalRequest.requested_at.desc())
                .limit(limit)
            ).scalars()
        )
        return [
            {
                "approval_id": r.id,
                "agent": r.agent_name,
                "title": r.title,
                "detail": r.detail,
                "value": str(r.value),
                "requested_at": r.requested_at.isoformat(),
                "expires_at": r.expires_at.isoformat() if r.expires_at else None,
                "run_id": r.run_id,
            }
            for r in rows
        ]


def resolve(
    approval_id: str,
    approved: bool,
    resolved_by: str,
    note: str | None = None,
    channel: str | None = None,
) -> dict[str, Any]:
    """Record a decision and resume the parked agent.

    Resolving twice is refused rather than replayed: a second approval would
    resume an already-finished graph and could re-send a purchase order.
    """
    with session_scope() as session:
        request = session.get(ApprovalRequest, approval_id)
        if request is None:
            raise KeyError(f"No approval request {approval_id}.")
        if request.status != ApprovalStatus.PENDING:
            raise ValueError(
                f"Approval {approval_id} is already {request.status.value}"
                + (f" (by {request.resolved_by})" if request.resolved_by else "")
                + "."
            )

        request.status = ApprovalStatus.APPROVED if approved else ApprovalStatus.REJECTED
        request.resolved_at = clock.utcnow()
        request.resolved_by = resolved_by
        request.resolution_note = note
        if channel:
            request.channel = channel

        thread_id = request.thread_id
        run_id = request.run_id
        agent_name = request.agent_name

        publish(
            Event(
                Topic.APPROVAL_RESOLVED,
                {"approval_id": approval_id, "approved": approved, "by": resolved_by},
                source_run_id=run_id,
            ),
            session=session,
        )

    from restaurant_ai.kernel.registry import get_agent
    from restaurant_ai.kernel.runner import resume_agent

    spec = get_agent(agent_name)
    outcome = resume_agent(
        spec,
        thread_id,
        {"approved": approved, "by": resolved_by, "note": note},
    )

    log.info(
        "approval resolved",
        approval_id=approval_id,
        approved=approved,
        by=resolved_by,
        agent=agent_name,
    )
    return {
        "approval_id": approval_id,
        "approved": approved,
        "resolved_by": resolved_by,
        "agent": agent_name,
        "run_id": run_id,
        "summary": outcome.summary,
        "error": outcome.error,
    }


def expire_stale() -> int:
    """Expire requests past their TTL, and fail the runs waiting on them."""
    expired = 0
    with session_scope() as session:
        rows = list(
            session.execute(
                select(ApprovalRequest).where(
                    ApprovalRequest.status == ApprovalStatus.PENDING,
                    ApprovalRequest.expires_at.isnot(None),
                    ApprovalRequest.expires_at < clock.utcnow(),
                )
            ).scalars()
        )
        for request in rows:
            request.status = ApprovalStatus.EXPIRED
            request.resolved_at = clock.utcnow()
            request.resolution_note = (
                "Expired without a decision. The proposal was not acted on; "
                "the agent will re-evaluate on its next run."
            )
            run = session.get(AgentRun, request.run_id)
            if run is not None and run.status == AgentRunStatus.AWAITING_APPROVAL:
                run.status = AgentRunStatus.REJECTED
                run.finished_at = clock.utcnow()
                run.summary = (run.summary or "") + " Approval expired; nothing was committed."
            expired += 1

    if expired:
        log.info("expired stale approvals", count=expired)
    return expired
