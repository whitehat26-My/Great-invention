"""The canonical domain event.

One envelope for everything agents emit, so a subscriber can route on ``topic``
without knowing which agent produced it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any

from restaurant_ai import clock


class Topic(StrEnum):
    """Every event the platform publishes."""

    ORDER_PLACED = "order.placed"
    ORDER_CLOSED = "order.closed"
    STOCK_DEDUCTED = "stock.deducted"
    STOCK_LOW = "stock.low"
    PURCHASE_ORDER_DRAFTED = "purchase_order.drafted"
    PURCHASE_ORDER_APPROVED = "purchase_order.approved"
    PURCHASE_ORDER_SENT = "purchase_order.sent"
    GOODS_RECEIVED = "goods.received"
    INVOICE_RECEIVED = "invoice.received"
    INVOICE_DISCREPANCY = "invoice.discrepancy"
    PREP_PLAN_READY = "prep_plan.ready"
    KDS_TICKETS_FIRED = "kds.tickets_fired"
    RESERVATION_CONFIRMED = "reservation.confirmed"
    TABLE_OVERRUNNING = "table.overrunning"
    REVIEW_RECEIVED = "review.received"
    REVIEW_ESCALATED = "review.escalated"
    PRICE_CHANGE_PROPOSED = "price.change_proposed"
    PRICE_CHANGED = "price.changed"
    ROSTER_PUBLISHED = "roster.published"
    SHIFT_SWAP_REQUESTED = "shift.swap_requested"
    RECONCILIATION_COMPLETE = "reconciliation.complete"
    RECONCILIATION_EXCEPTION = "reconciliation.exception"
    DAILY_REPORT_READY = "daily_report.ready"
    APPROVAL_REQUESTED = "approval.requested"
    APPROVAL_RESOLVED = "approval.resolved"


@dataclass
class Event:
    topic: Topic
    payload: dict[str, Any] = field(default_factory=dict)
    source_run_id: str | None = None
    occurred_at: datetime | None = None

    def __post_init__(self) -> None:
        if self.occurred_at is None:
            self.occurred_at = clock.utcnow()
