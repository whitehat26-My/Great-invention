"""Celery tasks.

Scheduled agent runs, asynchronous webhook processing, and housekeeping.

A scheduled run that parks for approval is not an error: the task completes and
the approval waits in Slack. That distinction matters because retrying an
already-parked run would draft the same purchase order twice.
"""

from __future__ import annotations

from typing import Any

from celery import shared_task
from celery.utils.log import get_task_logger

from restaurant_ai import clock
from restaurant_ai.db.base import session_scope
from restaurant_ai.worker.celery_app import celery_app

log = get_task_logger(__name__)


@celery_app.task(name="restaurant_ai.worker.tasks.run_scheduled_agent", bind=True, max_retries=2)
def run_scheduled_agent(self, agent_name: str, reason: str = "scheduled") -> dict[str, Any]:
    """Run one agent on its schedule."""
    from restaurant_ai.kernel.registry import get_agent
    from restaurant_ai.kernel.runner import run_agent

    try:
        spec = get_agent(agent_name)
    except KeyError as exc:
        log.error("unknown agent on schedule: %s", exc)
        return {"agent": agent_name, "status": "unknown_agent"}

    try:
        outcome = run_agent(spec, trigger="schedule", trigger_ref=reason)
    except Exception as exc:
        log.exception("scheduled run failed for %s", agent_name)
        raise self.retry(exc=exc, countdown=60) from exc

    if outcome.interrupted:
        # Parked for a human. Not a failure, and must not be retried.
        _notify_approval(outcome)
        return {
            "agent": agent_name,
            "status": "awaiting_approval",
            "run_id": outcome.run_id,
            "thread_id": outcome.thread_id,
        }

    return {
        "agent": agent_name,
        "status": "completed",
        "run_id": outcome.run_id,
        "summary": outcome.summary,
    }


@celery_app.task(name="restaurant_ai.worker.tasks.process_inbound_event", bind=True, max_retries=3)
def process_inbound_event(self, event_id: str, event_type: str) -> dict[str, Any]:
    """Process a recorded webhook payload."""
    from restaurant_ai.api.ingest import mark_processed
    from restaurant_ai.api.webhooks.handlers import HANDLERS
    from restaurant_ai.db.models import InboundEvent

    handler = HANDLERS.get(event_type)
    if handler is None:
        return {"event_id": event_id, "status": "no_handler"}

    with session_scope() as session:
        event = session.get(InboundEvent, event_id)
        if event is None:
            return {"event_id": event_id, "status": "missing"}
        if event.processed_at is not None:
            return {"event_id": event_id, "status": "already_processed"}
        payload = dict(event.payload or {})

    try:
        with session_scope() as session:
            result = handler(payload, session)
    except Exception as exc:
        log.exception("handler failed for %s", event_type)
        mark_processed(event_id, error=f"{type(exc).__name__}: {exc}")
        raise self.retry(exc=exc, countdown=30) from exc

    mark_processed(event_id)
    return {"event_id": event_id, "status": "processed", "result": result}


@celery_app.task(name="restaurant_ai.worker.tasks.drain_events")
def drain_events(limit: int = 200) -> dict[str, Any]:
    """Deliver queued domain events from the transactional outbox."""
    from restaurant_ai.events.bus import drain_outbox

    return {"delivered": drain_outbox(limit=limit)}


@celery_app.task(name="restaurant_ai.worker.tasks.expire_stale_approvals")
def expire_stale_approvals() -> dict[str, Any]:
    """Expire approval requests nobody answered.

    A purchase order approved three days late orders against demand that has
    moved on, so a stale request is closed rather than left to be rubber-stamped
    whenever someone next looks at Slack.
    """
    from restaurant_ai.approvals.service import expire_stale

    return {"expired": expire_stale()}


@celery_app.task(name="restaurant_ai.worker.tasks.deduct_stock")
def deduct_stock(order_id: str) -> dict[str, Any]:
    """Explode an order through its recipes and deduct the ingredients."""
    from restaurant_ai.api.webhooks.handlers import deduct_stock_for_order

    with session_scope() as session:
        count = deduct_stock_for_order(order_id, session)
    return {"order_id": order_id, "ingredients_deducted": count}


def _notify_approval(outcome) -> None:
    """Send the approval card for a run that parked."""
    try:
        from restaurant_ai.approvals.service import dispatch_pending_for_run

        dispatch_pending_for_run(outcome.run_id)
    except Exception:
        log.exception("could not dispatch approval notification")


@shared_task(name="restaurant_ai.worker.tasks.ping")
def ping() -> str:
    return f"pong {clock.now().isoformat()}"
