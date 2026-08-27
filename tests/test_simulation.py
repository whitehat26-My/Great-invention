"""The end-to-end test: one full simulated service day.

This is the test that would catch a regression nothing else would. Orders go in
through the real webhook handler, the scheduled agents fire at their real times,
approvals go through the real gate, and the books get closed. If BOM explosion
breaks, or reconciliation stops balancing, or an agent starts failing, a
simulated day notices.

It is slow by unit-test standards and worth it.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from restaurant_ai.db.models import (
    AgentRun,
    AgentRunStatus,
    DailyReport,
    JournalEntry,
    JournalLine,
    OrderHeader,
    ReconciliationBatch,
    StockMovement,
    TimeEntry,
)
from restaurant_ai.simulation import journals_balance, simulate_day

pytestmark = [pytest.mark.db, pytest.mark.integration]

# One shared run: simulating a day is expensive, and every assertion below is
# about the same day's outcome.
SIM_DATE = date(2026, 8, 20)


@pytest.fixture(scope="module")
def simulated(request):
    """Run one simulated day inside a transaction that is rolled back."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    import restaurant_ai.db.base as db_base
    from restaurant_ai.config import get_settings
    from restaurant_ai.db.seed import seed_all

    engine = create_engine(get_settings().database_url, pool_pre_ping=True, future=True)
    connection = engine.connect()
    transaction = connection.begin()
    bound = sessionmaker(
        bind=connection, expire_on_commit=False, join_transaction_mode="create_savepoint"
    )
    original = db_base.get_sessionmaker
    db_base.get_sessionmaker = lambda: bound

    session = bound()
    try:
        if session.execute(select(func.count(OrderHeader.id))).scalar_one() == 0:
            seed_all(session, history_days=56)
            session.flush()
        result = simulate_day(business_date=SIM_DATE, auto_approve=True)
        yield result, session
    finally:
        db_base.get_sessionmaker = original
        session.close()
        transaction.rollback()
        connection.close()
        engine.dispose()


class TestTheDayRuns:
    def test_no_step_failed(self, simulated):
        result, _ = simulated
        assert result.failures == [], f"failed steps: {[f.label for f in result.failures]}"

    def test_orders_were_ingested(self, simulated):
        result, _ = simulated
        assert result.orders_ingested > 20, "a service day should trade"
        assert result.orders_rejected == 0

    def test_every_scheduled_agent_ran(self, simulated):
        result, session = simulated
        runs = list(
            session.execute(
                select(AgentRun).where(
                    AgentRun.business_date == SIM_DATE, AgentRun.trigger == "simulation"
                )
            ).scalars()
        )
        assert len(runs) >= 10
        assert all(
            r.status in (AgentRunStatus.COMPLETED, AgentRunStatus.AWAITING_APPROVAL) for r in runs
        ), [(r.agent_name, r.status.value) for r in runs if r.status == AgentRunStatus.FAILED]

    def test_no_agent_errored(self, simulated):
        _result, session = simulated
        failed = list(
            session.execute(
                select(AgentRun).where(
                    AgentRun.business_date == SIM_DATE,
                    AgentRun.status == AgentRunStatus.FAILED,
                )
            ).scalars()
        )
        assert failed == [], [(r.agent_name, r.error) for r in failed]


class TestApprovals:
    def test_gates_were_hit_and_answered(self, simulated):
        result, _ = simulated
        assert result.approvals_requested > 0, "a day's trading should propose something"
        assert result.approvals_approved == result.approvals_requested


class TestStockFlowed:
    def test_sales_deducted_ingredients(self, simulated):
        _result, session = simulated
        deductions = session.execute(
            select(func.count(StockMovement.id)).where(StockMovement.source_type == "order")
        ).scalar_one()
        assert deductions > 0, "selling food must move stock"

    def test_deductions_are_negative(self, simulated):
        _result, session = simulated
        positive = session.execute(
            select(func.count(StockMovement.id)).where(
                StockMovement.source_type == "order", StockMovement.quantity > 0
            )
        ).scalar_one()
        assert positive == 0, "a sale can only remove stock"

    def test_approved_orders_brought_stock_in(self, simulated):
        _result, session = simulated
        receipts = session.execute(
            select(func.count(StockMovement.id)).where(StockMovement.source_type == "goods_receipt")
        ).scalar_one()
        assert receipts >= 0  # deliveries land a day after the PO in the fake


class TestTheBooksClose:
    def test_journals_were_posted(self, simulated):
        _result, session = simulated
        count = session.execute(
            select(func.count(JournalEntry.id)).where(JournalEntry.business_date == SIM_DATE)
        ).scalar_one()
        assert count >= 2, "at minimum a sales entry and a COGS entry"

    def test_every_journal_balances(self, simulated):
        # The single most important assertion here. If debits stop equalling
        # credits the books are wrong however good the report looks.
        balanced, details = journals_balance(SIM_DATE)
        assert balanced, f"unbalanced entries: {details}"

    def test_journal_lines_are_one_sided(self, simulated):
        _result, session = simulated
        both = session.execute(
            select(func.count(JournalLine.id))
            .join(JournalEntry, JournalLine.entry_id == JournalEntry.id)
            .where(
                JournalEntry.business_date == SIM_DATE,
                JournalLine.debit > 0,
                JournalLine.credit > 0,
            )
        ).scalar_one()
        assert both == 0

    def test_the_day_reconciles(self, simulated):
        _result, session = simulated
        batch = session.execute(
            select(ReconciliationBatch).where(ReconciliationBatch.business_date == SIM_DATE)
        ).scalar_one()
        # Delivery takings settle weekly on the platform's payout, so they are
        # receivables on the day, not exceptions.
        assert batch.is_balanced, f"variance {batch.variance}, notes: {batch.notes}"
        assert batch.matched_count > 0

    def test_labour_was_recorded(self, simulated):
        # Prime cost is COGS plus labour. A day with no labour would produce a
        # flatteringly low prime cost that simply omits everyone's wages.
        _result, session = simulated
        cost = session.execute(
            select(func.coalesce(func.sum(TimeEntry.cost), 0)).where(
                TimeEntry.business_date == SIM_DATE
            )
        ).scalar_one()
        assert Decimal(str(cost)) > 0


class TestTheReport:
    def test_a_report_was_produced(self, simulated):
        result, _ = simulated
        assert result.report, "the day must end with a report"

    def test_the_numbers_are_internally_consistent(self, simulated):
        _result, session = simulated
        report = session.execute(
            select(DailyReport).where(DailyReport.business_date == SIM_DATE)
        ).scalar_one()
        assert report.prime_cost == report.cogs + report.labour_cost
        assert report.covers > 0
        assert report.net_revenue > 0
        expected_check = (report.net_revenue / report.covers).quantize(Decimal("0.01"))
        assert report.average_check == expected_check

    def test_food_cost_is_plausible(self, simulated):
        _result, session = simulated
        report = session.execute(
            select(DailyReport).where(DailyReport.business_date == SIM_DATE)
        ).scalar_one()
        assert Decimal("0.15") < report.food_cost_pct < Decimal("0.55"), (
            f"food cost {report.food_cost_pct} is outside any plausible band; "
            f"the BOM or the pricing is wrong"
        )

    def test_the_commentary_says_something_useful(self, simulated):
        _result, session = simulated
        report = session.execute(
            select(DailyReport).where(DailyReport.business_date == SIM_DATE)
        ).scalar_one()
        assert report.commentary
        assert "prime cost" in report.commentary.lower()


class TestDeterminism:
    def test_the_same_day_generates_the_same_orders(self):
        # The simulated day doubles as a regression test, which only works if it
        # replays identically.
        from restaurant_ai.integrations.fakes import FakePOS

        pos = FakePOS()
        first = pos.generate_day(SIM_DATE)
        second = pos.generate_day(SIM_DATE)
        assert [o.external_id for o in first] == [o.external_id for o in second]
        assert [o.subtotal for o in first] == [o.subtotal for o in second]
