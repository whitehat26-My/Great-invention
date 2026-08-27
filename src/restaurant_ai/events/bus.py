"""The event bus, built on a transactional outbox.

Events are written to ``outbox_event`` in the same transaction as the state
change that produced them. If the process dies between committing a purchase
order and announcing it, the announcement is still on disk and goes out on the
next drain — rather than the two falling out of step, which is the failure mode
of publishing to a broker directly from application code.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from restaurant_ai import clock
from restaurant_ai.db.base import session_scope
from restaurant_ai.db.models import OutboxEvent
from restaurant_ai.events.schema import Event, Topic
from restaurant_ai.kernel.audit import _jsonable
from restaurant_ai.logging_setup import get_logger

log = get_logger(__name__)

# In-process subscribers, keyed by topic. The simulator and tests use these;
# a production deployment would drain the outbox to a real broker instead.
_subscribers: dict[str, list] = {}


def publish(
    event: Event, session: Session | None = None, dispatch_now: bool = False
) -> OutboxEvent:
    """Record an event. Joins the caller's transaction when given a session."""
    with session_scope(session) as db:
        row = OutboxEvent(
            topic=str(event.topic),
            payload=_jsonable(event.payload),
            created_at=event.occurred_at or clock.utcnow(),
            source_run_id=event.source_run_id,
        )
        db.add(row)
        db.flush()
        if dispatch_now:
            _deliver(event)
            row.dispatched_at = clock.utcnow()
        return row


def subscribe(topic: Topic, handler) -> None:
    _subscribers.setdefault(str(topic), []).append(handler)


def clear_subscribers() -> None:
    _subscribers.clear()


def drain_outbox(limit: int = 200, session: Session | None = None) -> int:
    """Deliver undispatched events. Safe to run repeatedly.

    A handler that raises leaves its event undispatched with the error recorded,
    so a failing subscriber retries rather than silently dropping the event.
    """
    delivered = 0
    with session_scope(session) as db:
        rows = list(
            db.execute(
                select(OutboxEvent)
                .where(OutboxEvent.dispatched_at.is_(None))
                .order_by(OutboxEvent.created_at)
                .limit(limit)
            ).scalars()
        )
        for row in rows:
            event = Event(
                topic=row.topic,  # type: ignore[arg-type]
                payload=row.payload or {},
                source_run_id=row.source_run_id,
                occurred_at=row.created_at,
            )
            try:
                _deliver(event)
            except Exception as exc:
                row.attempts += 1
                row.last_error = f"{type(exc).__name__}: {exc}"
                log.warning("outbox delivery failed", topic=row.topic, error=str(exc))
                continue
            row.dispatched_at = clock.utcnow()
            delivered += 1
    return delivered


def _deliver(event: Event) -> None:
    for handler in _subscribers.get(str(event.topic), []):
        handler(event)


def recent(topic: Topic | None = None, limit: int = 50) -> list[dict[str, Any]]:
    """Recent events, for the CLI and debugging."""
    with session_scope() as db:
        stmt = select(OutboxEvent).order_by(OutboxEvent.created_at.desc()).limit(limit)
        if topic is not None:
            stmt = stmt.where(OutboxEvent.topic == str(topic))
        return [
            {
                "topic": row.topic,
                "payload": row.payload,
                "created_at": row.created_at,
                "dispatched": row.dispatched_at is not None,
            }
            for row in db.execute(stmt).scalars()
        ]
