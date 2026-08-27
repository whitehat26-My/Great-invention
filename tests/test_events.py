"""The transactional outbox.

Events are written in the same transaction as the state change that produced
them, so a crash between committing a purchase order and announcing it cannot
leave the two out of step.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from restaurant_ai.db.models import OutboxEvent
from restaurant_ai.events import Event, Topic, publish
from restaurant_ai.events.bus import clear_subscribers, drain_outbox, subscribe

pytestmark = pytest.mark.db


@pytest.fixture(autouse=True)
def _clean_subscribers():
    clear_subscribers()
    yield
    clear_subscribers()


@pytest.fixture(autouse=True)
def _quiet_outbox(db):
    """Mark pre-existing events dispatched so a drain only sees this test's own.

    The outbox is shared state: trading, agent runs and the simulation all write
    to it. Without this, a test that drains and counts deliveries counts
    everyone else's events too. Rolled back with the rest of the transaction.
    """
    from restaurant_ai import clock

    db.execute(
        OutboxEvent.__table__.update()
        .where(OutboxEvent.dispatched_at.is_(None))
        .values(dispatched_at=clock.utcnow())
    )
    db.flush()


class TestPublish:
    def test_writes_to_the_outbox(self, db):
        # Returned directly rather than re-queried: the database holds events
        # from other activity on the same topic, and this asserts on the one
        # this test published.
        row = publish(Event(Topic.STOCK_LOW, {"ingredient": "chicken"}), session=db)
        assert row is not None
        assert row.payload["ingredient"] == "chicken"
        assert row.topic == str(Topic.STOCK_LOW)

    def test_joins_the_callers_transaction(self, db):
        # Written through the caller's session, so it lives or dies with the
        # state change beside it.
        row = publish(Event(Topic.ORDER_PLACED, {"order": "A-1"}), session=db)
        assert db.execute(select(OutboxEvent).where(OutboxEvent.id == row.id)).scalar_one_or_none()

    def test_undispatched_by_default(self, db):
        row = publish(Event(Topic.ORDER_PLACED, {}), session=db)
        assert row.dispatched_at is None

    def test_decimals_are_stored_as_strings(self, db):
        from decimal import Decimal

        # Money must not round-trip through binary floating point.
        row = publish(
            Event(Topic.PURCHASE_ORDER_DRAFTED, {"total": Decimal("1480.55")}), session=db
        )
        assert row.payload["total"] == "1480.55"


class TestDrain:
    def test_delivers_to_subscribers(self, db):
        seen: list[Event] = []
        subscribe(Topic.STOCK_LOW, seen.append)
        publish(Event(Topic.STOCK_LOW, {"ingredient": "prawns"}), session=db)
        db.flush()
        assert drain_outbox(session=db) >= 1
        assert seen and seen[0].payload["ingredient"] == "prawns"

    def test_marks_dispatched(self, db):
        row = publish(Event(Topic.ORDER_CLOSED, {}), session=db)
        db.flush()
        drain_outbox(session=db)
        db.flush()
        db.refresh(row)
        assert row.dispatched_at is not None

    def test_does_not_redeliver(self, db):
        seen: list[Event] = []
        subscribe(Topic.ORDER_CLOSED, seen.append)
        publish(Event(Topic.ORDER_CLOSED, {}), session=db)
        db.flush()
        drain_outbox(session=db)
        drain_outbox(session=db)
        assert len(seen) == 1

    def test_a_failing_handler_leaves_the_event_for_retry(self, db):
        def explode(event: Event) -> None:
            raise RuntimeError("downstream unavailable")

        subscribe(Topic.REVIEW_ESCALATED, explode)
        row = publish(Event(Topic.REVIEW_ESCALATED, {}), session=db)
        db.flush()
        drain_outbox(session=db)
        db.flush()
        db.refresh(row)
        assert row.dispatched_at is None, "a failed delivery must be retried, not dropped"
        assert row.attempts == 1
        assert "downstream unavailable" in row.last_error

    def test_only_matching_subscribers_are_called(self, db):
        stock: list[Event] = []
        orders: list[Event] = []
        subscribe(Topic.STOCK_LOW, stock.append)
        subscribe(Topic.ORDER_PLACED, orders.append)
        publish(Event(Topic.STOCK_LOW, {}), session=db)
        db.flush()
        drain_outbox(session=db)
        assert len(stock) == 1 and orders == []

    def test_no_subscribers_still_dispatches(self, db):
        row = publish(Event(Topic.DAILY_REPORT_READY, {}), session=db)
        db.flush()
        drain_outbox(session=db)
        db.flush()
        db.refresh(row)
        assert row.dispatched_at is not None
