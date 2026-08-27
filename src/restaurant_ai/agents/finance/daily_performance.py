"""Daily Performance Agent.

Runs at 23:45 and produces the end-of-day report: prime cost, labour ratio,
food cost, operating margin, and how the day compared with the forecast.

Prime cost (COGS plus labour) is the number that decides whether a restaurant
survives, so the report leads with it and states plainly whether the day was
sustainable rather than burying it in a table.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from pydantic import BaseModel, Field
from sqlalchemy import func, select

from restaurant_ai.agents.common import covers_on, units_sold_on
from restaurant_ai.db.models import (
    DailyReport,
    ItemForecast,
    MenuItem,
    MovementReason,
    OrderHeader,
    OrderStatus,
    PrepPlan,
    ReconciliationBatch,
    StockMovement,
    TimeEntry,
)
from restaurant_ai.domain.costing import cost_of_requirement, explode_many
from restaurant_ai.domain.reconciliation import compute_metrics
from restaurant_ai.events import Event, Topic, publish
from restaurant_ai.kernel.registry import register
from restaurant_ai.kernel.spec import AgentSpec, ToolContext, ToolSpec

ZERO = Decimal("0")


class ReportArgs(BaseModel):
    business_date: str | None = Field(None, description="ISO date; defaults to today.")


def perceive(context: ToolContext) -> dict[str, Any]:
    session = context.session
    day = context.business_date
    return {
        "business_date": day.isoformat(),
        "covers": covers_on(session, day),
        "orders": int(
            session.execute(
                select(func.count(OrderHeader.id)).where(
                    OrderHeader.business_date == day, OrderHeader.status != OrderStatus.VOID
                )
            ).scalar_one()
        ),
        "reconciled": session.execute(
            select(ReconciliationBatch).where(ReconciliationBatch.business_date == day)
        ).scalar_one_or_none()
        is not None,
    }


def compile_report(context: ToolContext, business_date: str | None = None) -> dict[str, Any]:
    """Compile the end-of-day performance report."""
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
        return {"compiled": False, "note": f"No trading on {day}."}

    net_revenue = sum((o.subtotal - o.discount for o in orders), ZERO)
    covers = sum((o.party_size for o in orders), 0)

    sold = units_sold_on(session, day)
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

    waste_movements = session.execute(
        select(StockMovement).where(
            StockMovement.reason == MovementReason.WASTE,
            func.date(StockMovement.occurred_at) == day,
        )
    ).scalars()
    waste_cost = sum((abs(m.quantity) * m.unit_cost for m in waste_movements), ZERO).quantize(
        Decimal("0.01")
    )

    metrics = compute_metrics(net_revenue, covers, cogs, labour, waste_cost)

    # How the day compared with what was forecast this morning.
    forecasts = list(
        session.execute(select(ItemForecast).where(ItemForecast.business_date == day)).scalars()
    )
    forecast_units = sum((f.forecast_qty for f in forecasts), ZERO)
    actual_units = sum(sold.values(), ZERO)
    plan = session.execute(
        select(PrepPlan).where(PrepPlan.business_date == day)
    ).scalar_one_or_none()

    variance_pct = (
        ((actual_units - forecast_units) / forecast_units).quantize(Decimal("0.0001"))
        if forecast_units > 0
        else ZERO
    )

    batch = session.execute(
        select(ReconciliationBatch).where(ReconciliationBatch.business_date == day)
    ).scalar_one_or_none()

    # Best and worst sellers by contribution, which is what a manager acts on.
    items = {
        i.id: i
        for i in session.execute(
            select(MenuItem).where(MenuItem.id.in_(list(sold) or [""]))
        ).scalars()
    }
    ranked = sorted(
        (
            {
                "name": items[i].name,
                "units": str(q),
                "revenue": str((items[i].price * q).quantize(Decimal("0.01"))),
            }
            for i, q in sold.items()
            if i in items
        ),
        key=lambda d: Decimal(d["revenue"]),
        reverse=True,
    )

    commentary = _commentary(metrics, variance_pct, batch, plan)

    report = session.execute(
        select(DailyReport).where(DailyReport.business_date == day)
    ).scalar_one_or_none()
    if report is None:
        report = DailyReport(business_date=day)
        session.add(report)

    report.run_id = context.run_id
    report.net_revenue = metrics.net_revenue
    report.covers = metrics.covers
    report.average_check = metrics.average_check
    report.cogs = metrics.cogs
    report.labour_cost = metrics.labour_cost
    report.prime_cost = metrics.prime_cost
    report.prime_cost_pct = metrics.prime_cost_pct
    report.labour_pct = metrics.labour_pct
    report.food_cost_pct = metrics.food_cost_pct
    report.operating_margin_pct = metrics.operating_margin_pct
    report.waste_cost = metrics.waste_cost
    report.commentary = commentary
    session.flush()

    publish(
        Event(
            Topic.DAILY_REPORT_READY,
            {"business_date": day.isoformat(), "net_revenue": str(metrics.net_revenue)},
            source_run_id=context.run_id,
        ),
        session=session,
    )

    return {
        "compiled": True,
        "business_date": day.isoformat(),
        "net_revenue": str(metrics.net_revenue),
        "covers": metrics.covers,
        "average_check": str(metrics.average_check),
        "cogs": str(metrics.cogs),
        "food_cost_pct": f"{metrics.food_cost_pct * 100:.1f}%",
        "labour_cost": str(metrics.labour_cost),
        "labour_pct": f"{metrics.labour_pct * 100:.1f}%",
        "prime_cost": str(metrics.prime_cost),
        "prime_cost_pct": f"{metrics.prime_cost_pct * 100:.1f}%",
        "operating_margin_pct": f"{metrics.operating_margin_pct * 100:.1f}%",
        "waste_cost": str(metrics.waste_cost),
        "forecast_units": str(forecast_units),
        "actual_units": str(actual_units),
        "forecast_variance_pct": f"{variance_pct * 100:+.1f}%",
        "reconciliation": (
            {
                "balanced": batch.is_balanced,
                "variance": str(batch.variance),
                "exceptions": batch.unmatched_count,
            }
            if batch
            else None
        ),
        "top_sellers": ranked[:5],
        "slowest": ranked[-3:] if len(ranked) > 3 else [],
        "commentary": commentary,
    }


def render_report(context: ToolContext, business_date: str | None = None) -> dict[str, Any]:
    """Render the report as text for Slack or email."""
    session = context.session
    day = (
        __import__("datetime").date.fromisoformat(business_date)
        if business_date
        else context.business_date
    )
    report = session.execute(
        select(DailyReport).where(DailyReport.business_date == day)
    ).scalar_one_or_none()
    if report is None:
        return {"rendered": False, "note": f"No report compiled for {day}."}

    lines = [
        f"End of day - {day:%A %d %B %Y}",
        "",
        f"  Net revenue      {report.net_revenue:>12,.2f}",
        f"  Covers           {report.covers:>12}",
        f"  Average check    {report.average_check:>12,.2f}",
        "",
        f"  COGS             {report.cogs:>12,.2f}   ({report.food_cost_pct * 100:.1f}% of revenue)",
        f"  Labour           {report.labour_cost:>12,.2f}   ({report.labour_pct * 100:.1f}% of revenue)",
        f"  Prime cost       {report.prime_cost:>12,.2f}   ({report.prime_cost_pct * 100:.1f}% of revenue)",
        f"  Waste            {report.waste_cost:>12,.2f}",
        "",
        f"  Operating margin {report.operating_margin_pct * 100:>11.1f}%",
        "",
        report.commentary or "",
    ]
    return {"rendered": True, "text": "\n".join(lines), "business_date": day.isoformat()}


def _commentary(metrics, variance_pct: Decimal, batch, plan) -> str:
    """Plain-language read on the day, leading with what matters."""
    parts = [metrics.verdict()]

    if variance_pct > Decimal("0.10"):
        parts.append(
            f"Demand ran {variance_pct * 100:.0f}% above forecast, so the kitchen was working "
            f"short on prep. Worth raising tomorrow's plan."
        )
    elif variance_pct < Decimal("-0.10"):
        parts.append(
            f"Demand came in {abs(variance_pct) * 100:.0f}% below forecast. Check tonight's "
            f"waste log before prepping to the same level again."
        )

    if metrics.waste_cost > metrics.cogs * Decimal("0.05") and metrics.cogs > 0:
        parts.append(
            f"Waste at {metrics.waste_cost} is over 5% of food cost, which is high enough to "
            f"be worth tracing to a station."
        )

    if batch is not None and not batch.is_balanced:
        parts.append(
            f"Cash-up does not tie: {batch.variance:+.2f} unexplained across "
            f"{batch.unmatched_count} item(s). Needs a manager before the banking run."
        )

    return " ".join(parts)


def autonomous(context: ToolContext, perceived: dict[str, Any]) -> dict[str, Any]:
    return {
        "summary": (
            f"Compiling the end-of-day report for {perceived.get('business_date')}: "
            f"{perceived.get('covers')} covers across {perceived.get('orders')} orders."
        ),
        "results": {},
        "tool_calls": [
            {"name": "compile_report", "args": {}},
            {"name": "render_report", "args": {}},
        ],
    }


DAILY_PERFORMANCE_AGENT = register(
    AgentSpec(
        name="daily_performance",
        person="Camelia",
        department="finance",
        title="Daily Performance Agent",
        description=(
            "Compiles end-of-day reports analysing labour-to-revenue ratios, prime costs "
            "(COGS plus labour) and operating margins."
        ),
        system_prompt=(
            "You are the Daily Performance Agent for a restaurant. You write the end-of-day "
            "report the owner reads with their coffee.\n\n"
            "Lead with prime cost - COGS plus labour as a share of revenue. It is the number "
            "that decides whether the business survives: under 60% is healthy, over 70% means "
            "every cover is losing money once rent is counted. Say which it was.\n\n"
            "Then explain what moved it. A bad day caused by one quiet Tuesday is not the same "
            "as a bad day caused by food cost drifting up all month, and the owner needs to "
            "know which they are looking at.\n\n"
            "Be direct about bad numbers. A report that softens a 72% prime cost is worse than "
            "no report. Keep it short enough to read standing up."
        ),
        model_tier="reasoning",
        tools=[
            ToolSpec(
                name="compile_report",
                description="Compute the day's revenue, prime cost, ratios and forecast variance.",
                fn=compile_report,
                args_schema=ReportArgs,
            ),
            ToolSpec(
                name="render_report",
                description="Render the compiled report as text for Slack or email.",
                fn=render_report,
                args_schema=ReportArgs,
            ),
        ],
        perceive=perceive,
        autonomous=autonomous,
    )
)
