"""Daily reconciliation and the double-entry postings.

Three money trails have to agree at the end of a service day:

    what the POS says was sold
    what the card processor and delivery platforms actually settled
    what landed in the bank

They never agree exactly. Cards settle the next day, delivery platforms pay
weekly net of commission, cash has counting errors, and tips pass through
without being revenue. Matching is therefore tolerance-based and staged: exact
reference match first, then amount-and-date, then fuzzy within tolerance, and
anything left over is an exception for a human rather than a silent plug.

The one rule that is never bent: journals balance. An entry that does not is
rejected here rather than written and reconciled later.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal

ZERO = Decimal("0")
CENT = Decimal("0.01")

# Amounts within this are treated as the same money (rounding, minor fees).
DEFAULT_TOLERANCE = Decimal("0.05")
# Card settlements normally land the next business day.
SETTLEMENT_WINDOW_DAYS = 3


@dataclass
class InternalRecord:
    """Something we believe we are owed or hold: a payment or a platform payout."""

    record_id: str
    record_type: str  # "payment" | "delivery_payout"
    amount: Decimal
    business_date: date
    reference: str | None = None
    method: str = ""
    matched: bool = False


@dataclass
class StatementLine:
    """A line from the bank or merchant statement."""

    line_id: str
    amount: Decimal
    posted_on: date
    description: str
    reference: str | None = None
    matched: bool = False


@dataclass
class MatchPair:
    internal: InternalRecord | None
    statement: StatementLine | None
    amount: Decimal
    difference: Decimal
    strategy: str
    is_exception: bool = False
    reason: str = ""


@dataclass
class ReconciliationResult:
    business_date: date
    matches: list[MatchPair] = field(default_factory=list)
    pos_total: Decimal = ZERO
    card_settled: Decimal = ZERO
    cash_counted: Decimal = ZERO
    delivery_net: Decimal = ZERO
    variance: Decimal = ZERO

    @property
    def matched(self) -> list[MatchPair]:
        return [m for m in self.matches if not m.is_exception]

    @property
    def exceptions(self) -> list[MatchPair]:
        return [m for m in self.matches if m.is_exception]

    @property
    def is_balanced(self) -> bool:
        return abs(self.variance) <= DEFAULT_TOLERANCE

    def summary(self) -> str:
        status = "balanced" if self.is_balanced else f"variance {self.variance:+.2f}"
        return (
            f"{self.business_date}: POS {self.pos_total:.2f}, settled "
            f"{self.card_settled + self.cash_counted + self.delivery_net:.2f}, {status}. "
            f"{len(self.matched)} matched, {len(self.exceptions)} exception(s)."
        )


def reconcile(
    internal: list[InternalRecord],
    statement: list[StatementLine],
    business_date: date,
    tolerance: Decimal = DEFAULT_TOLERANCE,
) -> ReconciliationResult:
    """Match internal records against statement lines, staged strictest first."""
    result = ReconciliationResult(business_date=business_date)
    internal = [InternalRecord(**vars(r)) for r in internal]  # work on copies
    statement = [StatementLine(**vars(s)) for s in statement]

    _match_by_reference(internal, statement, result)
    _match_by_amount_and_date(internal, statement, result, tolerance)
    _match_by_amount_within_window(internal, statement, result, tolerance)

    for record in internal:
        if record.matched:
            continue
        result.matches.append(
            MatchPair(
                internal=record,
                statement=None,
                amount=record.amount,
                difference=record.amount,
                strategy="unmatched_internal",
                is_exception=True,
                reason=(
                    f"{record.record_type} {record.reference or record.record_id} for "
                    f"{record.amount:.2f} has no corresponding statement line. Either it has "
                    f"not settled yet or it was never captured."
                ),
            )
        )

    for line in statement:
        if line.matched:
            continue
        result.matches.append(
            MatchPair(
                internal=None,
                statement=line,
                amount=line.amount,
                difference=-line.amount,
                strategy="unmatched_statement",
                is_exception=True,
                reason=(
                    f"Statement line '{line.description}' for {line.amount:.2f} on "
                    f"{line.posted_on} has no internal record. Possible unrecorded takings, "
                    f"a fee, or a chargeback."
                ),
            )
        )

    return result


def _match_by_reference(
    internal: list[InternalRecord], statement: list[StatementLine], result: ReconciliationResult
) -> None:
    """Exact processor-reference match: unambiguous, so do it first."""
    by_reference: dict[str, StatementLine] = {
        line.reference: line for line in statement if line.reference
    }
    for record in internal:
        if record.matched or not record.reference:
            continue
        line = by_reference.get(record.reference)
        if line is None or line.matched:
            continue
        difference = (record.amount - line.amount).quantize(CENT)
        record.matched = line.matched = True
        result.matches.append(
            MatchPair(
                internal=record,
                statement=line,
                amount=record.amount,
                difference=difference,
                strategy="reference",
                is_exception=difference != ZERO,
                reason=(
                    f"Reference matched but amounts differ by {difference:+.2f}"
                    if difference != ZERO
                    else ""
                ),
            )
        )


def _match_by_amount_and_date(
    internal: list[InternalRecord],
    statement: list[StatementLine],
    result: ReconciliationResult,
    tolerance: Decimal,
) -> None:
    """Same amount, same day."""
    for record in internal:
        if record.matched:
            continue
        for line in statement:
            if line.matched or line.posted_on != record.business_date:
                continue
            difference = (record.amount - line.amount).quantize(CENT)
            if abs(difference) > tolerance:
                continue
            record.matched = line.matched = True
            result.matches.append(
                MatchPair(
                    internal=record,
                    statement=line,
                    amount=record.amount,
                    difference=difference,
                    strategy="amount_date",
                )
            )
            break


def _match_by_amount_within_window(
    internal: list[InternalRecord],
    statement: list[StatementLine],
    result: ReconciliationResult,
    tolerance: Decimal,
) -> None:
    """Same amount within the settlement window — cards land a day or two late."""
    for record in internal:
        if record.matched:
            continue
        window_end = record.business_date + timedelta(days=SETTLEMENT_WINDOW_DAYS)
        for line in statement:
            if line.matched:
                continue
            if not (record.business_date <= line.posted_on <= window_end):
                continue
            difference = (record.amount - line.amount).quantize(CENT)
            if abs(difference) > tolerance:
                continue
            record.matched = line.matched = True
            lag = (line.posted_on - record.business_date).days
            result.matches.append(
                MatchPair(
                    internal=record,
                    statement=line,
                    amount=record.amount,
                    difference=difference,
                    strategy="amount_window",
                    reason=f"Settled {lag} day(s) after the business date.",
                )
            )
            break


# --- Double-entry -----------------------------------------------------------


@dataclass
class JournalLineSpec:
    account_code: str
    debit: Decimal = ZERO
    credit: Decimal = ZERO
    memo: str = ""


@dataclass
class JournalEntrySpec:
    """A balanced entry, ready to post. Validated before it can be written."""

    business_date: date
    memo: str
    source: str
    lines: list[JournalLineSpec] = field(default_factory=list)
    source_id: str | None = None

    @property
    def total_debit(self) -> Decimal:
        return sum((line.debit for line in self.lines), ZERO).quantize(CENT)

    @property
    def total_credit(self) -> Decimal:
        return sum((line.credit for line in self.lines), ZERO).quantize(CENT)

    @property
    def is_balanced(self) -> bool:
        return self.total_debit == self.total_credit

    def validate(self) -> None:
        if not self.lines:
            raise ValueError("Journal entry has no lines")
        for line in self.lines:
            if line.debit < 0 or line.credit < 0:
                raise ValueError(f"Negative amount on {line.account_code}")
            if (line.debit == ZERO) == (line.credit == ZERO):
                raise ValueError(
                    f"Line on {line.account_code} must be exactly one of debit or credit"
                )
        if not self.is_balanced:
            raise ValueError(
                f"Entry does not balance: debits {self.total_debit} != credits {self.total_credit}"
            )


def build_sales_entry(
    business_date: date,
    food_sales: Decimal,
    beverage_sales: Decimal,
    tax: Decimal,
    cash: Decimal,
    card: Decimal,
    delivery: Decimal,
    discounts: Decimal = ZERO,
    source_id: str | None = None,
) -> JournalEntrySpec:
    """The day's takings.

    Debit where the money went (cash drawer, card receivable, platform
    receivable), credit what earned it (food, beverage) plus the tax collected
    on behalf of the government, which is a liability rather than revenue.
    """
    entry = JournalEntrySpec(
        business_date=business_date,
        memo=f"Daily sales for {business_date}",
        source="bookkeeping_agent",
        source_id=source_id,
    )
    if cash > 0:
        entry.lines.append(JournalLineSpec("1000", debit=cash, memo="Cash takings"))
    if card > 0:
        entry.lines.append(JournalLineSpec("1020", debit=card, memo="Card settlements due"))
    if delivery > 0:
        entry.lines.append(
            JournalLineSpec("1030", debit=delivery, memo="Delivery platform receivable")
        )
    if discounts > 0:
        entry.lines.append(JournalLineSpec("4900", debit=discounts, memo="Discounts given"))

    if food_sales > 0:
        entry.lines.append(JournalLineSpec("4000", credit=food_sales, memo="Food sales"))
    if beverage_sales > 0:
        entry.lines.append(JournalLineSpec("4010", credit=beverage_sales, memo="Beverage sales"))
    if tax > 0:
        entry.lines.append(JournalLineSpec("2100", credit=tax, memo="Sales tax collected"))

    _balance_with_variance(entry, "Cash/settlement variance")
    return entry


def build_cogs_entry(
    business_date: date, cogs: Decimal, waste: Decimal = ZERO, source_id: str | None = None
) -> JournalEntrySpec:
    """Move consumed and wasted stock out of inventory into expense."""
    entry = JournalEntrySpec(
        business_date=business_date,
        memo=f"Cost of goods sold for {business_date}",
        source="bookkeeping_agent",
        source_id=source_id,
    )
    if cogs > 0:
        entry.lines.append(JournalLineSpec("5000", debit=cogs, memo="COGS"))
    if waste > 0:
        entry.lines.append(JournalLineSpec("5100", debit=waste, memo="Food waste"))
    total = cogs + waste
    if total > 0:
        entry.lines.append(JournalLineSpec("1200", credit=total, memo="Inventory consumed"))
    return entry


def build_labour_entry(
    business_date: date, labour_cost: Decimal, source_id: str | None = None
) -> JournalEntrySpec:
    entry = JournalEntrySpec(
        business_date=business_date,
        memo=f"Labour for {business_date}",
        source="bookkeeping_agent",
        source_id=source_id,
    )
    if labour_cost > 0:
        entry.lines.append(JournalLineSpec("6000", debit=labour_cost, memo="Wages"))
        entry.lines.append(JournalLineSpec("2000", credit=labour_cost, memo="Wages payable"))
    return entry


def build_delivery_settlement_entry(
    business_date: date,
    platform: str,
    gross: Decimal,
    commission: Decimal,
    net: Decimal,
    source_id: str | None = None,
) -> JournalEntrySpec:
    """A platform payout: cash in, commission expensed, receivable cleared."""
    entry = JournalEntrySpec(
        business_date=business_date,
        memo=f"{platform} settlement for {business_date}",
        source="bookkeeping_agent",
        source_id=source_id,
    )
    if net > 0:
        entry.lines.append(JournalLineSpec("1010", debit=net, memo=f"{platform} payout received"))
    if commission > 0:
        entry.lines.append(JournalLineSpec("6100", debit=commission, memo=f"{platform} commission"))
    if gross > 0:
        entry.lines.append(
            JournalLineSpec("1030", credit=gross, memo=f"{platform} receivable cleared")
        )
    _balance_with_variance(entry, f"{platform} settlement adjustment")
    return entry


def _balance_with_variance(entry: JournalEntrySpec, memo: str) -> None:
    """Post any residual to the variance account so the entry balances.

    Real money never reconciles to the cent. Rather than refusing to post, the
    difference goes somewhere explicit and visible that a human reviews, instead
    of being hidden inside a revenue line.
    """
    difference = (entry.total_debit - entry.total_credit).quantize(CENT)
    if difference == ZERO:
        return
    if difference > 0:
        entry.lines.append(JournalLineSpec("6900", credit=difference, memo=memo))
    else:
        entry.lines.append(JournalLineSpec("6900", debit=-difference, memo=memo))


# --- Performance metrics ----------------------------------------------------


@dataclass
class PerformanceMetrics:
    net_revenue: Decimal
    covers: int
    average_check: Decimal
    cogs: Decimal
    labour_cost: Decimal
    prime_cost: Decimal
    prime_cost_pct: Decimal
    labour_pct: Decimal
    food_cost_pct: Decimal
    operating_margin_pct: Decimal
    waste_cost: Decimal = ZERO

    def verdict(self) -> str:
        """Plain-language read on the day, against industry benchmarks.

        Prime cost is the number that decides whether a restaurant survives:
        under 60% is healthy, over 70% is losing money on every cover.
        """
        notes: list[str] = []
        if self.prime_cost_pct <= Decimal("0.60"):
            notes.append(f"Prime cost {self.prime_cost_pct * 100:.1f}% is healthy (target <=60%).")
        elif self.prime_cost_pct <= Decimal("0.70"):
            notes.append(
                f"Prime cost {self.prime_cost_pct * 100:.1f}% is above the 60% target but "
                f"recoverable."
            )
        else:
            notes.append(
                f"Prime cost {self.prime_cost_pct * 100:.1f}% is unsustainable. Every cover is "
                f"losing money once fixed costs are counted."
            )
        if self.food_cost_pct > Decimal("0.35"):
            notes.append(
                f"Food cost {self.food_cost_pct * 100:.1f}% is high; check portioning, waste "
                f"and supplier pricing."
            )
        if self.labour_pct > Decimal("0.32"):
            notes.append(
                f"Labour {self.labour_pct * 100:.1f}% of revenue suggests the roster was heavy "
                f"for the covers achieved."
            )
        return " ".join(notes)


def compute_metrics(
    net_revenue: Decimal,
    covers: int,
    cogs: Decimal,
    labour_cost: Decimal,
    waste_cost: Decimal = ZERO,
    fixed_costs: Decimal = ZERO,
) -> PerformanceMetrics:
    """Derive the ratios that actually run a restaurant."""
    prime = (cogs + labour_cost).quantize(CENT)

    def pct(value: Decimal) -> Decimal:
        return (value / net_revenue).quantize(Decimal("0.0001")) if net_revenue > 0 else ZERO

    return PerformanceMetrics(
        net_revenue=net_revenue.quantize(CENT),
        covers=covers,
        average_check=(net_revenue / Decimal(covers)).quantize(CENT) if covers else ZERO,
        cogs=cogs.quantize(CENT),
        labour_cost=labour_cost.quantize(CENT),
        prime_cost=prime,
        prime_cost_pct=pct(prime),
        labour_pct=pct(labour_cost),
        food_cost_pct=pct(cogs),
        operating_margin_pct=pct(net_revenue - prime - fixed_costs),
        waste_cost=waste_cost.quantize(CENT),
    )
