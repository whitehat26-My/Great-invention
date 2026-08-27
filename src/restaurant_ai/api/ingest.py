"""Idempotent event ingestion.

Every webhook payload is recorded in ``inbound_event``, keyed on the provider's
own event id, before anything acts on it. Two consequences worth having:
a redelivered webhook is a no-op rather than a duplicate sale, and a handler
that crashes can be retried from the stored body instead of the event being
lost.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from restaurant_ai import clock
from restaurant_ai.db.base import session_scope
from restaurant_ai.db.models import InboundEvent
from restaurant_ai.logging_setup import get_logger

log = get_logger(__name__)


class IngestResult:
    def __init__(self, event_id: str | None, duplicate: bool) -> None:
        self.event_id = event_id
        self.duplicate = duplicate

    def as_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "status": "duplicate" if self.duplicate else "accepted",
        }


def record_event(
    provider: str,
    event_type: str,
    external_id: str,
    payload: dict[str, Any],
    session: Session | None = None,
) -> IngestResult:
    """Store a webhook payload, returning whether it was already seen.

    The uniqueness check is the database constraint, not a prior SELECT: two
    concurrent deliveries of the same event would both pass a read check and
    both insert.
    """
    with session_scope(session) as db:
        event = InboundEvent(
            provider=provider,
            event_type=event_type,
            external_id=external_id,
            payload=payload,
            received_at=clock.utcnow(),
        )
        db.add(event)
        try:
            db.flush()
        except IntegrityError:
            db.rollback()
            log.info("duplicate webhook ignored", provider=provider, external_id=external_id)
            return IngestResult(event_id=None, duplicate=True)
        return IngestResult(event_id=event.id, duplicate=False)


def mark_processed(event_id: str, error: str | None = None, session: Session | None = None) -> None:
    with session_scope(session) as db:
        event = db.get(InboundEvent, event_id)
        if event is None:
            return
        event.processed_at = clock.utcnow()
        event.processing_error = error
