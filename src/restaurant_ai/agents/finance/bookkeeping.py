"""Bookkeeping & Reconciliation Agent.

Runs at 23:30 and squares the day: POS takings against card settlements,
delivery-platform payouts and the bank, then posts the double-entry journals.

Matching is staged strictest-first — exact processor reference, then amount and
date, then amount within the settlement window — and anything left over becomes
an exception for a human. Nothing is plugged to make the day balance; the
residual goes to an explicit variance account where someone will see it.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from pydantic import BaseModel, Field
from sqlalchemy import func, select

from restaurant_ai import clock
from restaurant_ai.agents.common import units_sold_on
from restaurant_ai.db.models import (
    BankTransaction,
    DeliveryPayout,
    JournalEntry,
    JournalLine,
    LedgerAccount,
    MenuItem,
    OrderHeader,
    OrderStatus,
    Payment,
    PaymentMethod,
    ReconciliationBatch,
    ReconciliationMatch,
    TimeEntry,
)
from restaurant_ai.domain.costing import cost_of_requirement, explode_many
from restaurant_ai.domain.reconciliation import (
    InternalRecord,
    StatementLine,
    build_cogs_entry,
    build_labour_entry,
    build_sales_entry,
    reconcile,
)
from restaurant_ai.events import Event, Topic, publish
from restaurant_ai.kernel.registry import register
from restaurant_ai.kernel.spec import AgentSpec, ToolContext, ToolSpec

ZERO = Decimal("0")


class ReconcileArgs(BaseModel):
    business_date: str | None = Field(None, description="ISO date; defaults to today.")


def perceive(context: ToolContext) -> dict[str, Any]:
    session = context.session
    day = context.business_date

    takings = session.execute(
        select(func.coalesce(func.sum(Payment.amount), 0)).where(Payment.business_date == day)
    ).scalar_one()
    orders = session.execute(
        select(func.count(OrderHeader.id)).where(
            OrderHeader.business_date == day, OrderHeader.status != OrderStatus.VOID
        )
    ).scalar_one()
    existing = session.execute(
        select(ReconciliationBatch).where(ReconciliationBatch.business_date == day)
    ).scalar_one_or_none()

    return {
        "business_date": day.isoformat(),
        "orders": int(orders),
        "takings": str(Decimal(str(takings))),
        "already_reconciled": existing is not None,
    }


def reconcile_day(context: ToolContext, business_date: str | None = None) -> dict[str, Any]:
    """Match the day's takings against what actually settled."""
    from restaurant_ai.integrations import get_integrations

    session = context.session
    day = (
        __import__("datetime").date.fromisoformat(business_date)
        if business_date
        else context.business_date
    )
    bank = get_integrations().bank

    payments = list(session.execute(select(Payment).where(Payment.business_date == day)).scalars())
    if not payments:
        return {"reconciled": False, "note": f"No payments recorded on {day}."}

    # Three kinds of money settle three different ways, and only one of them
    # appears on today's merchant statement:
    #
    #   cash              never settles electronically; counted, not matched
    #   card / e-wallet   settles via the acquirer within a day or two
    #   delivery platform settles weekly, net of commission, on the platform's
    #                     own payout - so on the day of trading it is an
    #                     outstanding receivable, NOT an unmatched exception
    #
    # Matching delivery takings against the card statement reported an entire
    # day of platform sales as unreconciled every single night.
    cash_total = sum((p.amount + p.tip for p in payments if p.method == PaymentMethod.CASH), ZERO)
    delivery_outstanding = sum(
        (p.amount + p.tip for p in payments if p.method == PaymentMethod.DELIVERY_PLATFORM),
        ZERO,
    )
    electronic = [
        p for p in payments if p.method not in (PaymentMethod.CASH, PaymentMethod.DELIVERY_PLATFORM)
    ]

    internal = [
        InternalRecord(
            record_id=p.id,
            record_type="payment",
            amount=(p.amount + p.tip),
            business_date=p.business_date,
            reference=p.processor_ref,
            method=p.method.value,
        )
        for p in electronic
    ]

    statement_lines = bank.fetch_settlements(day)
    for line in statement_lines:
        existing = session.execute(
            select(BankTransaction).where(BankTransaction.statement_ref == line.reference)
        ).scalar_one_or_none()
        if existing is None:
            session.add(
                BankTransaction(
                    statement_ref=line.reference,
                    posted_on=line.posted_on,
                    description=line.description,
                    amount=line.amount,
                    counterparty=line.counterparty,
                )
            )
    session.flush()

    statement = [
        StatementLine(
            line_id=line.reference,
            amount=line.amount,
            posted_on=line.posted_on,
            description=line.description,
            reference=line.reference,
        )
        for line in statement_lines
    ]

    result = reconcile(internal, statement, day)

    payouts = list(
        session.execute(
            select(DeliveryPayout).where(
                DeliveryPayout.period_start <= day, DeliveryPayout.period_end >= day
            )
        ).scalars()
    )
    delivery_net = sum((p.net_payout for p in payouts), ZERO)

    card_settled = sum((m.amount for m in result.matched), ZERO)
    pos_total = sum((p.amount + p.tip for p in payments), ZERO)
    result.pos_total = pos_total
    result.card_settled = card_settled
    result.cash_counted = cash_total
    result.delivery_net = delivery_net
    # Everything the POS recorded must be accounted for as settled, counted, or
    # legitimately still owed to us by a delivery platform.
    result.variance = (pos_total - card_settled - cash_total - delivery_outstanding).quantize(
        Decimal("0.01")
    )

    batch = session.execute(
        select(ReconciliationBatch).where(ReconciliationBatch.business_date == day)
    ).scalar_one_or_none()
    if batch is None:
        batch = ReconciliationBatch(business_date=day)
        session.add(batch)
        session.flush()
    else:
        for match in list(batch.matches):
            session.delete(match)
        session.flush()

    batch.run_id = context.run_id
    batch.pos_total = pos_total
    batch.card_settled = card_settled
    batch.cash_counted = cash_total
    batch.delivery_net = delivery_net
    batch.variance = result.variance
    batch.matched_count = len(result.matched)
    batch.unmatched_count = len(result.exceptions)
    batch.is_balanced = result.is_balanced
    batch.notes = result.summary() + (
        f" {delivery_outstanding:.2f} of delivery takings await the platform payout."
        if delivery_outstanding > 0
        else ""
    )

    for pair in result.matches:
        session.add(
            ReconciliationMatch(
                batch_id=batch.id,
                left_type=pair.internal.record_type if pair.internal else "statement",
                left_id=pair.internal.record_id if pair.internal else "",
                right_type="bank_transaction" if pair.statement else None,
                right_id=pair.statement.line_id if pair.statement else None,
                amount=pair.amount,
                difference=pair.difference,
                is_exception=pair.is_exception,
                reason=pair.reason or None,
            )
        )

    for payment in electronic:
        payment.is_reconciled = any(
            m.internal and m.internal.record_id == payment.id and not m.is_exception
            for m in result.matches
        )

    session.flush()
    publish(
        Event(
            Topic.RECONCILIATION_COMPLETE,
            {"business_date": day.isoformat(), "balanced": result.is_balanced},
            source_run_id=context.run_id,
        ),
        session=session,
    )
    if result.exceptions:
        publish(
            Event(
                Topic.RECONCILIATION_EXCEPTION,
                {"business_date": day.isoformat(), "count": len(result.exceptions)},
                source_run_id=context.run_id,
            ),
            session=session,
        )

    return {
        "reconciled": True,
        "business_date": day.isoformat(),
        "pos_total": str(pos_total),
        "card_settled": str(card_settled),
        "cash_counted": str(cash_total),
        "delivery_outstanding": str(delivery_outstanding),
        "delivery_net": str(delivery_net),
        "variance": str(result.variance),
        "balanced": result.is_balanced,
        "matched": len(result.matched),
        "exceptions": len(result.exceptions),
        "exception_detail": [
            {"amount": str(m.amount), "strategy": m.strategy, "reason": m.reason}
            for m in result.exceptions[:15]
        ],
        "summary": result.summary(),
    }


def post_journals(context: ToolContext, business_date: str | None = None) -> dict[str, Any]:
    """Post the day's sales, COGS and labour journals."""
    session = context.session
    day = (
        __import__("datetime").date.fromisoformat(business_date)
        if business_date
        else context.business_date
    )

    orders = list(
        session.execute(
            select(OrderHeader).where(
                OrderHeader.business_date == day, OrderHeader.status != OrderStatus.VOID
            )
        ).scalars()
    )
    if not orders:
        return {"posted": 0, "note": f"No trading on {day}."}

    payments = list(session.execute(select(Payment).where(Payment.business_date == day)).scalars())

    # Split revenue between food and beverage: drinks are course 4.
    sold = units_sold_on(session, day)
    items = {
        i.id: i
        for i in session.execute(
            select(MenuItem).where(MenuItem.id.in_(list(sold) or [""]))
        ).scalars()
    }
    food = sum(
        (items[i].price * q for i, q in sold.items() if i in items and items[i].course != 4), ZERO
    )
    beverage = sum(
        (items[i].price * q for i, q in sold.items() if i in items and items[i].course == 4), ZERO
    )

    tax = sum((o.tax for o in orders), ZERO)
    discounts = sum((o.discount for o in orders), ZERO)
    cash = sum((p.amount + p.tip for p in payments if p.method == PaymentMethod.CASH), ZERO)
    card = sum(
        (
            p.amount + p.tip
            for p in payments
            if p.method in (PaymentMethod.CARD, PaymentMethod.EWALLET)
        ),
        ZERO,
    )
    delivery = sum(
        (p.amount + p.tip for p in payments if p.method == PaymentMethod.DELIVERY_PLATFORM), ZERO
    )

    cogs = cost_of_requirement(session, explode_many(session, sold))
    labour = sum(
        (
            e.cost
            for e in session.execute(
                select(TimeEntry).where(TimeEntry.business_date == day)
            ).scalars()
        ),
        ZERO,
    )

    specs = [
        build_sales_entry(
            day, food, beverage, tax, cash, card, delivery, discounts, source_id=context.run_id
        ),
        build_cogs_entry(day, cogs, source_id=context.run_id),
    ]
    if labour > 0:
        specs.append(build_labour_entry(day, labour, source_id=context.run_id))

    accounts = {a.code: a for a in session.execute(select(LedgerAccount)).scalars()}
    posted: list[dict[str, Any]] = []

    for index, spec in enumerate(specs):
        if not spec.lines:
            continue
        spec.validate()  # refuses to post anything that does not balance

        entry_number = f"JE-{day.strftime('%y%m%d')}-{index + 1:03d}"
        existing = session.execute(
            select(JournalEntry).where(JournalEntry.entry_number == entry_number)
        ).scalar_one_or_none()
        if existing is not None:
            continue  # already posted; journals are never rewritten

        entry = JournalEntry(
            entry_number=entry_number,
            business_date=day,
            posted_at=clock.utcnow(),
            memo=spec.memo,
            source=spec.source,
            source_id=spec.source_id,
        )
        session.add(entry)
        session.flush()

        for line in spec.lines:
            account = accounts.get(line.account_code)
            if account is None:
                continue
            session.add(
                JournalLine(
                    entry_id=entry.id,
                    account_id=account.id,
                    debit=line.debit,
                    credit=line.credit,
                    memo=line.memo,
                )
            )
        posted.append(
            {
                "entry_number": entry_number,
                "memo": spec.memo,
                "debits": str(spec.total_debit),
                "credits": str(spec.total_credit),
                "balanced": spec.is_balanced,
            }
        )

    session.flush()
    return {
        "posted": len(posted),
        "business_date": day.isoformat(),
        "food_sales": str(food),
        "beverage_sales": str(beverage),
        "cogs": str(cogs),
        "labour": str(labour),
        "entries": posted,
        "all_balanced": all(e["balanced"] for e in posted),
    }


def autonomous(context: ToolContext, perceived: dict[str, Any]) -> dict[str, Any]:
    return {
        "summary": (
            f"Reconciling {perceived.get('orders', 0)} order(s) taking "
            f"{perceived.get('takings')} on {perceived.get('business_date')}, "
            f"then posting the day's journals."
        ),
        "results": {},
        "tool_calls": [
            {"name": "reconcile_day", "args": {}},
            {"name": "post_journals", "args": {}},
        ],
    }


BOOKKEEPING_AGENT = register(
    AgentSpec(
        name="bookkeeping",
        person="Emil",
        department="finance",
        title="Bookkeeping & Reconciliation Agent",
        description=(
            "Reconciles daily POS takings, delivery platform payouts and card transactions "
            "against merchant statements."
        ),
        system_prompt=(
            "You are the Bookkeeping and Reconciliation Agent for a restaurant.\n\n"
            "Every night you square three money trails that never quite agree: what the POS "
            "recorded, what the processors and platforms settled, and what reached the bank. "
            "Cards settle a day late, platforms pay weekly net of commission, and cash has "
            "counting errors.\n\n"
            "Match strictest-first: exact processor reference, then amount and date, then "
            "amount within the settlement window. What will not match is an exception for a "
            "human, not something to force.\n\n"
            "Never plug a difference to make the day balance. Post the residual to the "
            "variance account where someone will see it, and say what is unexplained. "
            "Journals must balance to the cent, and a posted journal is never edited - "
            "corrections are new reversing entries."
        ),
        model_tier="reasoning",
        tools=[
            ToolSpec(
                name="reconcile_day",
                description="Match the day's takings against card settlements and payouts.",
                fn=reconcile_day,
                args_schema=ReconcileArgs,
            ),
            ToolSpec(
                name="post_journals",
                description="Post the day's sales, COGS and labour journals in double entry.",
                fn=post_journals,
                args_schema=ReconcileArgs,
            ),
        ],
        perceive=perceive,
        autonomous=autonomous,
    )
)
