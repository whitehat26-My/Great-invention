"""Webhook endpoints.

Each endpoint does the same four things: verify the signature over the raw
bytes, record the payload idempotently, dispatch the work, and acknowledge.

Dispatch goes to Celery when a broker is reachable and runs inline otherwise, so
the platform works in a single process for development and the simulator
without changing the ingestion path being exercised.
"""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status

from restaurant_ai.api.ingest import mark_processed, record_event
from restaurant_ai.api.security import require_signature
from restaurant_ai.api.webhooks.handlers import HANDLERS
from restaurant_ai.db.base import session_scope
from restaurant_ai.logging_setup import get_logger

log = get_logger(__name__)
router = APIRouter(prefix="/webhooks", tags=["webhooks"])


def _ingest(provider: str, event_type: str, body: bytes) -> dict[str, Any]:
    try:
        payload = json.loads(body or b"{}")
    except json.JSONDecodeError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=f"Malformed JSON: {exc}"
        ) from exc

    external_id = (
        payload.get("event_id")
        or payload.get("external_id")
        or payload.get("order_id")
        or payload.get("payout_ref")
    )
    if not external_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "Payload needs an event_id (or external_id/order_id/payout_ref) so "
                "redelivery can be detected."
            ),
        )

    result = record_event(provider, event_type, str(external_id), payload)
    if result.duplicate:
        return {**result.as_dict(), "note": "Already processed; nothing done."}

    outcome = _dispatch(event_type, payload, result.event_id)
    return {**result.as_dict(), **outcome}


def _dispatch(event_type: str, payload: dict[str, Any], event_id: str | None) -> dict[str, Any]:
    """Hand the work to Celery, or run it inline when no broker is reachable."""
    from restaurant_ai.worker.celery_app import broker_available

    if broker_available():
        from restaurant_ai.worker.tasks import process_inbound_event

        process_inbound_event.delay(event_id, event_type)
        return {"dispatched": "queued"}

    handler = HANDLERS.get(event_type)
    if handler is None:
        return {"dispatched": "ignored", "reason": f"No handler for {event_type!r}."}

    try:
        with session_scope() as session:
            outcome = handler(payload, session)
        mark_processed(event_id) if event_id else None
        return {"dispatched": "inline", "result": outcome}
    except Exception as exc:
        log.error("inline handler failed", event_type=event_type, error=str(exc))
        if event_id:
            mark_processed(event_id, error=f"{type(exc).__name__}: {exc}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Handler failed: {exc}",
        ) from exc


@router.post("/pos", status_code=status.HTTP_202_ACCEPTED)
async def pos_webhook(body: bytes = Depends(require_signature)) -> dict[str, Any]:
    """A sale from the point of sale. Deducts stock via the recipe BOM."""
    return _ingest("pos", "pos.order", body)


@router.post("/payments", status_code=status.HTTP_202_ACCEPTED)
async def payments_webhook(body: bytes = Depends(require_signature)) -> dict[str, Any]:
    """A settlement notification from the payment gateway."""
    return _ingest("payments", "payment.settled", body)


@router.post("/reviews", status_code=status.HTTP_202_ACCEPTED)
async def reviews_webhook(body: bytes = Depends(require_signature)) -> dict[str, Any]:
    """A new review from a review platform."""
    return _ingest("reviews", "review.posted", body)


@router.post("/delivery", status_code=status.HTTP_202_ACCEPTED)
async def delivery_webhook(body: bytes = Depends(require_signature)) -> dict[str, Any]:
    """A payout notification from a delivery platform."""
    return _ingest("delivery", "delivery.payout", body)


@router.post("/whatsapp", status_code=status.HTTP_202_ACCEPTED)
async def whatsapp_webhook(body: bytes = Depends(require_signature)) -> dict[str, Any]:
    """An inbound guest message; the reservation agent reads these on its sweep."""
    return _ingest("whatsapp", "message.received", body)
