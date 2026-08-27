"""Order routing and pacing, with tickets actually in flight.

Every other agent test ran against a quiet kitchen, which is how a crash in the
re-fire guard went unnoticed: with no tickets in the database the failing line
was never reached. The pacing agent runs every five minutes through service, so
a failure there stops the pass receiving tickets for the rest of the night.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy import func, select

from restaurant_ai import clock
from restaurant_ai.db.models import (
    AgentAction,
    AgentRun,
    AgentRunStatus,
    KdsTicket,
    MenuItem,
    OrderChannel,
    OrderHeader,
    OrderLine,
    OrderStatus,
)
from restaurant_ai.kernel.registry import get_agent
from restaurant_ai.kernel.runner import ephemeral_checkpointer, run_agent

pytestmark = pytest.mark.db


@pytest.fixture
def open_order(db):
    """One open two-course order waiting to be fired."""
    items = list(
        db.execute(
            select(MenuItem).where(MenuItem.sku.in_(["MNU-ROTIJALA", "MNU-GRILCHOP"]))
        ).scalars()
    )
    order = OrderHeader(
        order_number="TEST-PACE-1",
        channel=OrderChannel.DINE_IN,
        status=OrderStatus.OPEN,
        party_size=2,
        placed_at=clock.now(),
        business_date=clock.today(),
        subtotal=Decimal("41.80"),
        tax=Decimal("2.51"),
        total=Decimal("44.31"),
    )
    db.add(order)
    db.flush()
    for item in items:
        db.add(
            OrderLine(
                order_id=order.id,
                menu_item_id=item.id,
                quantity=Decimal("1"),
                unit_price=item.price,
                line_total=item.price,
                course=item.course,
            )
        )
    db.flush()
    return order


def _run():
    with ephemeral_checkpointer() as cp:
        return run_agent(get_agent("order_pacing"), trigger="test", checkpointer=cp)


class TestFiring:
    def test_fires_tickets_for_open_lines(self, db, open_order):
        outcome = _run()
        assert outcome.results["fire_tickets"]["tickets"] == 2

    def test_writes_kds_tickets(self, db, open_order):
        _run()
        db.expire_all()
        tickets = list(
            db.execute(select(KdsTicket).where(KdsTicket.order_id == open_order.id)).scalars()
        )
        assert len(tickets) == 2
        assert all(t.fire_at is not None for t in tickets)

    def test_marks_the_order_fired(self, db, open_order):
        _run()
        db.expire_all()
        assert db.get(OrderHeader, open_order.id).status == OrderStatus.FIRED

    def test_courses_are_sequenced(self, db, open_order):
        # The starter must plate before the main.
        _run()
        db.expire_all()
        tickets = {
            t.course: t
            for t in db.execute(
                select(KdsTicket).where(KdsTicket.order_id == open_order.id)
            ).scalars()
        }
        assert tickets[1].fire_at <= tickets[2].fire_at


class TestReFireGuard:
    """Regression: this is the case that crashed.

    A single-column select's .scalars() yields the values, not rows, so the
    guard was doing `str.order_line_id` and raising AttributeError the moment
    any ticket existed.
    """

    def test_a_second_run_does_not_error(self, db, open_order):
        _run()
        second = _run()
        assert second.error is None
        action = db.execute(
            select(AgentAction)
            .join(AgentRun, AgentAction.run_id == AgentRun.id)
            .where(AgentAction.tool_name == "fire_tickets")
            .order_by(AgentAction.occurred_at.desc())
            .limit(1)
        ).scalar_one()
        assert action.error is None, f"the re-fire guard raised: {action.error}"

    def test_a_second_run_does_not_duplicate_tickets(self, db, open_order):
        _run()
        db.expire_all()
        first = db.execute(
            select(func.count(KdsTicket.id)).where(KdsTicket.order_id == open_order.id)
        ).scalar_one()

        _run()
        db.expire_all()
        second = db.execute(
            select(func.count(KdsTicket.id)).where(KdsTicket.order_id == open_order.id)
        ).scalar_one()

        assert second == first, "an already-fired line must not be fired again"

    def test_a_second_run_says_so(self, db, open_order):
        _run()
        outcome = _run()
        assert "already been fired" in outcome.results["fire_tickets"]["note"]


class TestFailureIsReported:
    """A run whose tools all failed must not report clean success.

    The pacing crash was invisible precisely because the run said it had routed
    the tickets while the tool underneath had raised.
    """

    def test_a_failing_tool_marks_the_run_failed(self, db, open_order, monkeypatch):
        def explode(context, **kwargs):
            raise RuntimeError("KDS terminal unreachable")

        spec = get_agent("order_pacing")
        monkeypatch.setattr(spec.tool("fire_tickets"), "fn", explode)

        outcome = _run()
        db.expire_all()
        run = db.get(AgentRun, outcome.run_id)
        assert run.status == AgentRunStatus.FAILED

    def test_the_summary_names_the_failed_tool(self, db, open_order, monkeypatch):
        def explode(context, **kwargs):
            raise RuntimeError("KDS terminal unreachable")

        spec = get_agent("order_pacing")
        monkeypatch.setattr(spec.tool("fire_tickets"), "fn", explode)

        outcome = _run()
        assert "fire_tickets" in outcome.summary
        assert "not done" in outcome.summary
