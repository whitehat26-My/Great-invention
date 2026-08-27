from datetime import date
from decimal import Decimal

import pytest

from restaurant_ai.domain.reconciliation import (
    InternalRecord,
    JournalEntrySpec,
    JournalLineSpec,
    StatementLine,
    build_cogs_entry,
    build_delivery_settlement_entry,
    build_labour_entry,
    build_sales_entry,
    compute_metrics,
    reconcile,
)

D = Decimal
DAY = date(2026, 8, 27)


def _internal(rid: str, amount: str, ref: str | None = None, day: date = DAY) -> InternalRecord:
    return InternalRecord(
        record_id=rid, record_type="payment", amount=D(amount), business_date=day, reference=ref
    )


def _line(lid: str, amount: str, ref: str | None = None, day: date = DAY) -> StatementLine:
    return StatementLine(
        line_id=lid, amount=D(amount), posted_on=day, description=f"line {lid}", reference=ref
    )


class TestMatching:
    def test_exact_reference_match(self):
        result = reconcile(
            [_internal("p1", "100.00", "TXN-1")], [_line("s1", "100.00", "TXN-1")], DAY
        )
        assert len(result.matched) == 1
        assert result.matched[0].strategy == "reference"

    def test_reference_match_with_amount_difference_is_an_exception(self):
        # Same transaction, different money: that needs a human, not a silent pass.
        result = reconcile(
            [_internal("p1", "100.00", "TXN-1")], [_line("s1", "97.00", "TXN-1")], DAY
        )
        assert len(result.exceptions) == 1
        assert result.exceptions[0].difference == D("3.00")

    def test_amount_and_date_match_without_reference(self):
        result = reconcile([_internal("p1", "100.00")], [_line("s1", "100.00")], DAY)
        assert result.matched[0].strategy == "amount_date"

    def test_settlement_lands_a_day_late(self):
        # Cards routinely settle the next business day.
        result = reconcile(
            [_internal("p1", "100.00")], [_line("s1", "100.00", day=date(2026, 8, 28))], DAY
        )
        assert len(result.matched) == 1
        assert result.matched[0].strategy == "amount_window"
        assert "1 day" in result.matched[0].reason

    def test_settlement_beyond_the_window_is_not_matched(self):
        result = reconcile(
            [_internal("p1", "100.00")], [_line("s1", "100.00", day=date(2026, 9, 30))], DAY
        )
        assert len(result.exceptions) == 2  # unmatched on both sides

    def test_small_rounding_difference_is_tolerated(self):
        result = reconcile([_internal("p1", "100.00")], [_line("s1", "100.02")], DAY)
        assert len(result.matched) == 1

    def test_difference_beyond_tolerance_is_not_matched(self):
        result = reconcile([_internal("p1", "100.00")], [_line("s1", "150.00")], DAY)
        assert len(result.matched) == 0
        assert len(result.exceptions) == 2

    def test_unmatched_internal_is_reported_with_a_reason(self):
        result = reconcile([_internal("p1", "100.00", "TXN-9")], [], DAY)
        assert result.exceptions[0].strategy == "unmatched_internal"
        assert "no corresponding statement line" in result.exceptions[0].reason

    def test_unmatched_statement_is_reported(self):
        result = reconcile([], [_line("s1", "42.00")], DAY)
        assert result.exceptions[0].strategy == "unmatched_statement"
        assert "no internal record" in result.exceptions[0].reason

    def test_each_line_is_used_at_most_once(self):
        # Two identical payments and one statement line: exactly one match.
        result = reconcile(
            [_internal("p1", "100.00"), _internal("p2", "100.00")], [_line("s1", "100.00")], DAY
        )
        assert len(result.matched) == 1
        assert len(result.exceptions) == 1

    def test_reference_match_takes_priority_over_amount(self):
        # p1 must pair with its own reference, not the same-amount line.
        result = reconcile(
            [_internal("p1", "100.00", "TXN-1")],
            [_line("s_amount", "100.00"), _line("s_ref", "100.00", "TXN-1")],
            DAY,
        )
        matched = result.matched[0]
        assert matched.statement.line_id == "s_ref"

    def test_does_not_mutate_the_caller_s_records(self):
        records = [_internal("p1", "100.00")]
        reconcile(records, [_line("s1", "100.00")], DAY)
        assert records[0].matched is False

    def test_summary_reads_cleanly(self):
        result = reconcile([_internal("p1", "100.00")], [_line("s1", "100.00")], DAY)
        assert "matched" in result.summary()


class TestJournalValidation:
    def test_balanced_entry_validates(self):
        entry = JournalEntrySpec(
            business_date=DAY,
            memo="test",
            source="test",
            lines=[JournalLineSpec("1000", debit=D("10")), JournalLineSpec("4000", credit=D("10"))],
        )
        entry.validate()
        assert entry.is_balanced

    def test_unbalanced_entry_is_rejected(self):
        entry = JournalEntrySpec(
            business_date=DAY,
            memo="test",
            source="test",
            lines=[JournalLineSpec("1000", debit=D("10")), JournalLineSpec("4000", credit=D("7"))],
        )
        with pytest.raises(ValueError, match="does not balance"):
            entry.validate()

    def test_line_with_both_sides_is_rejected(self):
        entry = JournalEntrySpec(
            business_date=DAY,
            memo="test",
            source="test",
            lines=[JournalLineSpec("1000", debit=D("10"), credit=D("10"))],
        )
        with pytest.raises(ValueError, match="exactly one of debit or credit"):
            entry.validate()

    def test_line_with_neither_side_is_rejected(self):
        entry = JournalEntrySpec(
            business_date=DAY, memo="t", source="t", lines=[JournalLineSpec("1000")]
        )
        with pytest.raises(ValueError, match="exactly one of debit or credit"):
            entry.validate()

    def test_negative_amount_is_rejected(self):
        entry = JournalEntrySpec(
            business_date=DAY,
            memo="t",
            source="t",
            lines=[JournalLineSpec("1000", debit=D("-5")), JournalLineSpec("4000", credit=D("-5"))],
        )
        with pytest.raises(ValueError, match="Negative"):
            entry.validate()

    def test_empty_entry_is_rejected(self):
        with pytest.raises(ValueError, match="no lines"):
            JournalEntrySpec(business_date=DAY, memo="t", source="t").validate()


class TestEntryBuilders:
    def test_sales_entry_balances(self):
        entry = build_sales_entry(
            DAY,
            food_sales=D("1000.00"),
            beverage_sales=D("200.00"),
            tax=D("72.00"),
            cash=D("300.00"),
            card=D("700.00"),
            delivery=D("272.00"),
        )
        entry.validate()
        assert entry.is_balanced

    def test_sales_entry_posts_residual_to_variance_not_revenue(self):
        # Real cash never ties out exactly. The difference must land somewhere
        # explicit rather than being buried in a sales line.
        entry = build_sales_entry(
            DAY,
            food_sales=D("1000.00"),
            beverage_sales=D("0"),
            tax=D("60.00"),
            cash=D("1000.00"),
            card=D("0"),
            delivery=D("0"),
        )
        entry.validate()
        variance = [line for line in entry.lines if line.account_code == "6900"]
        assert variance, "residual must be posted to the variance account"
        assert entry.is_balanced

    def test_tax_is_a_liability_not_revenue(self):
        entry = build_sales_entry(DAY, D("100.00"), D("0"), D("6.00"), D("106.00"), D("0"), D("0"))
        tax_lines = [line for line in entry.lines if line.account_code == "2100"]
        assert len(tax_lines) == 1
        assert tax_lines[0].credit == D("6.00")

    def test_cogs_entry_balances(self):
        entry = build_cogs_entry(DAY, cogs=D("400.00"), waste=D("25.00"))
        entry.validate()
        inventory = [line for line in entry.lines if line.account_code == "1200"]
        assert inventory[0].credit == D("425.00")

    def test_labour_entry_balances(self):
        entry = build_labour_entry(DAY, D("650.00"))
        entry.validate()
        assert entry.total_debit == D("650.00")

    def test_delivery_settlement_expenses_commission(self):
        entry = build_delivery_settlement_entry(
            DAY, "GrabFood", gross=D("1000.00"), commission=D("300.00"), net=D("700.00")
        )
        entry.validate()
        commission = [line for line in entry.lines if line.account_code == "6100"]
        assert commission[0].debit == D("300.00")

    def test_zero_values_produce_no_lines(self):
        entry = build_cogs_entry(DAY, cogs=D("0"), waste=D("0"))
        assert entry.lines == []


class TestMetrics:
    def test_prime_cost_is_cogs_plus_labour(self):
        m = compute_metrics(D("10000"), 200, D("3000"), D("2800"))
        assert m.prime_cost == D("5800.00")
        assert m.prime_cost_pct == D("0.5800")

    def test_average_check(self):
        m = compute_metrics(D("10000"), 200, D("3000"), D("2800"))
        assert m.average_check == D("50.00")

    def test_zero_covers_does_not_divide_by_zero(self):
        assert compute_metrics(D("0"), 0, D("0"), D("0")).average_check == D("0")

    def test_healthy_prime_cost_verdict(self):
        assert "healthy" in compute_metrics(D("10000"), 200, D("3000"), D("2500")).verdict()

    def test_unsustainable_prime_cost_verdict(self):
        assert "unsustainable" in compute_metrics(D("10000"), 200, D("4000"), D("3500")).verdict()

    def test_high_labour_is_called_out(self):
        assert "Labour" in compute_metrics(D("10000"), 200, D("2000"), D("3500")).verdict()
