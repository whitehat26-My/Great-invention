"""The audit trail.

An autonomous system that spends money has to be answerable after the fact. For
any purchase order, price change or published review response, this records
which agent did it, on what trigger, what it was reasoning from, which tool
performed it and who approved it.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from restaurant_ai import clock
from restaurant_ai.db.models import AgentAction, AgentRun, AgentRunStatus
from restaurant_ai.kernel.state import ActionRecord


def new_thread_id(agent_name: str, business_date: date) -> str:
    """A stable-ish handle for the LangGraph checkpoint of one run.

    The uuid suffix keeps two runs of the same agent on the same day from
    colliding, which matters because an interrupted run resumes by thread id.
    """
    return f"{agent_name}:{business_date.isoformat()}:{uuid.uuid4().hex[:8]}"


def start_run(
    session: Session,
    agent_name: str,
    department: str,
    business_date: date,
    thread_id: str,
    trigger: str = "manual",
    trigger_ref: str | None = None,
    model: str | None = None,
    context: dict[str, Any] | None = None,
) -> AgentRun:
    run = AgentRun(
        agent_name=agent_name,
        department=department,
        trigger=trigger,
        trigger_ref=trigger_ref,
        status=AgentRunStatus.RUNNING,
        business_date=business_date,
        started_at=clock.utcnow(),
        thread_id=thread_id,
        model=model,
        context=_jsonable(context or {}),
    )
    session.add(run)
    session.flush()
    return run


def record_actions(session: Session, run_id: str, actions: list[ActionRecord]) -> int:
    """Persist this run's tool calls, continuing the existing sequence."""
    if not actions:
        return 0
    existing = session.execute(select(AgentAction).where(AgentAction.run_id == run_id)).scalars()
    start = max((a.sequence for a in existing), default=-1) + 1

    for offset, action in enumerate(actions):
        session.add(
            AgentAction(
                run_id=run_id,
                sequence=start + offset,
                tool_name=action.tool_name,
                arguments=_jsonable(action.arguments),
                result=_jsonable(action.result),
                is_proposal=action.is_proposal,
                error=action.error,
                occurred_at=action.occurred_at or clock.utcnow(),
                committed_at=None if action.is_proposal else clock.utcnow(),
            )
        )
    session.flush()
    return len(actions)


def finish_run(
    session: Session,
    run_id: str,
    status: AgentRunStatus,
    summary: str | None = None,
    error: str | None = None,
    results: dict[str, Any] | None = None,
) -> AgentRun | None:
    run = session.get(AgentRun, run_id)
    if run is None:
        return None
    run.status = status
    run.finished_at = clock.utcnow()
    if summary is not None:
        run.summary = summary
    if error is not None:
        run.error = error
    if results is not None:
        merged = dict(run.context or {})
        merged["results"] = _jsonable(results)
        run.context = merged
    session.flush()
    return run


def mark_awaiting_approval(session: Session, run_id: str) -> None:
    run = session.get(AgentRun, run_id)
    if run is not None:
        run.status = AgentRunStatus.AWAITING_APPROVAL
        session.flush()


def _jsonable(value: Any) -> Any:
    """Coerce to something JSONB will accept.

    Decimals become strings rather than floats: these are money and quantity
    figures, and round-tripping them through binary floating point would
    quietly corrupt the audit record.
    """
    from decimal import Decimal

    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(v) for v in value]
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if hasattr(value, "__dict__"):
        return _jsonable(vars(value))
    return str(value)
