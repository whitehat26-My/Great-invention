"""Dynamic Prep Forecaster.

Runs at 06:00 and answers one question: how much of each ingredient should the
kitchen prep today?

The chain is forecast per menu item, explode through the recipe BOM to
ingredients, subtract what is already on hand, then gross up for yield loss —
prepping 10 kg of onion when 15% is lost to peel and trim leaves the line short.

It also scores yesterday's forecast against what actually sold and feeds the
resulting bias back in. Without that loop the model repeats the same error every
day and the kitchen quietly learns to ignore it.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, Field
from sqlalchemy import select

from restaurant_ai.agents.common import active_menu_items, on_hand_all, sales_history, units_sold_on
from restaurant_ai.db.models import Ingredient, ItemForecast, PrepPlan, PrepPlanLine
from restaurant_ai.domain.costing import cost_of_requirement, explode_many
from restaurant_ai.domain.forecasting import forecast_day, learn_bias, score_accuracy
from restaurant_ai.events import Event, Topic, publish
from restaurant_ai.kernel.registry import register
from restaurant_ai.kernel.spec import AgentSpec, ToolContext, ToolSpec

ZERO = Decimal("0")

SYSTEM_PROMPT = """You are the Dynamic Prep Forecaster for a restaurant kitchen.

Every morning you tell the kitchen how much to prep. Getting it wrong is
expensive in both directions: over-prep goes in the bin at the end of service,
under-prep means 86ing a dish on a Friday night.

How you think about it:
- Day of week dominates. A Saturday is not a Tuesday, and averaging them serves
  neither.
- Respect shelf life. Prepping four days of a sauce that keeps two days is waste
  with extra steps.
- Gross up for yield. Trim, peel and cook-off losses are real; a net requirement
  is not a prep quantity.
- Learn from yesterday. If you under-forecast a dish three days running, correct
  for it rather than repeating the error.

Give the kitchen quantities in the units they actually work in, and say what
changed from a normal day for that weekday."""


class ForecastArgs(BaseModel):
    lookback_days: int = Field(56, description="Days of sales history to learn from.")
    event_factor: str = Field(
        "1.0", description="Multiplier for a known event, holiday or promotion."
    )


class ScoreArgs(BaseModel):
    pass


def perceive(context: ToolContext) -> dict[str, Any]:
    session = context.session
    yesterday = context.business_date - timedelta(days=1)
    history = sales_history(session, 56, until=context.business_date)

    existing = session.execute(
        select(PrepPlan).where(PrepPlan.business_date == context.business_date)
    ).scalar_one_or_none()

    return {
        "target_date": context.business_date.isoformat(),
        "weekday": context.business_date.strftime("%A"),
        "history_days": len({row.business_date for row in history}),
        "history_rows": len(history),
        "plan_exists": existing is not None,
        "yesterday_units": int(sum(units_sold_on(session, yesterday).values(), ZERO)),
    }


def score_yesterday(context: ToolContext) -> dict[str, Any]:
    """Compare yesterday's forecast with what actually sold."""
    session = context.session
    yesterday = context.business_date - timedelta(days=1)

    forecasts = list(
        session.execute(
            select(ItemForecast).where(ItemForecast.business_date == yesterday)
        ).scalars()
    )
    if not forecasts:
        return {"scored": 0, "note": f"No forecast on record for {yesterday}."}

    actual = units_sold_on(session, yesterday)
    forecast_map = {f.menu_item_id: f.forecast_qty for f in forecasts}
    errors = score_accuracy(forecast_map, actual)

    for row in forecasts:
        row.actual_qty = actual.get(row.menu_item_id, ZERO)
        row.abs_error = errors.get(row.menu_item_id, ZERO)
    session.flush()

    return {
        "scored": len(forecasts),
        "business_date": yesterday.isoformat(),
        "mape": str(errors["__mape__"]),
        "bias": str(errors["__bias__"]),
        "interpretation": (
            "Under-forecast: demand exceeded the plan."
            if errors["__bias__"] > Decimal("1.05")
            else "Over-forecast: prepped more than sold."
            if errors["__bias__"] < Decimal("0.95")
            else "Forecast tracked actual demand closely."
        ),
    }


def build_prep_plan(
    context: ToolContext, lookback_days: int = 56, event_factor: str = "1.0"
) -> dict[str, Any]:
    """Forecast demand, explode to ingredients, and write the prep list."""
    session = context.session
    target = context.business_date
    history = sales_history(session, lookback_days, until=target)
    if not history:
        return {"lines": 0, "note": "No sales history; cannot forecast."}

    # Yesterday's error becomes today's correction.
    yesterday = target - timedelta(days=1)
    previous = list(
        session.execute(
            select(ItemForecast).where(ItemForecast.business_date == yesterday)
        ).scalars()
    )
    bias: dict[str, Decimal] = {}
    if previous:
        actual = units_sold_on(session, yesterday)
        bias = learn_bias({f.menu_item_id: f.forecast_qty for f in previous}, actual)

    result = forecast_day(history, target, event_factor=Decimal(event_factor), bias=bias)
    if not result.items:
        return {"lines": 0, "note": "Forecast produced no items."}

    items = {i.id: i for i in active_menu_items(session)}
    quantities = {
        item_id: entry.forecast_qty for item_id, entry in result.items.items() if item_id in items
    }

    # Persist the per-item forecast so tomorrow can score it.
    session.execute(ItemForecast.__table__.delete().where(ItemForecast.business_date == target))
    for item_id, entry in result.items.items():
        if item_id not in items:
            continue
        session.add(
            ItemForecast(
                business_date=target,
                menu_item_id=item_id,
                forecast_qty=entry.forecast_qty,
                method=entry.method,
            )
        )

    requirement = explode_many(session, quantities)
    balances = on_hand_all(session)
    ingredients = {
        i.id: i
        for i in session.execute(
            select(Ingredient).where(Ingredient.id.in_(list(requirement) or [""]))
        ).scalars()
    }

    plan = session.execute(
        select(PrepPlan).where(PrepPlan.business_date == target)
    ).scalar_one_or_none()
    if plan is None:
        plan = PrepPlan(business_date=target)
        session.add(plan)
        session.flush()
    else:
        for line in list(plan.lines):
            session.delete(line)
        session.flush()

    forecast_revenue = sum(
        (items[item_id].price * qty for item_id, qty in quantities.items()), ZERO
    )
    plan.run_id = context.run_id
    plan.forecast_covers = result.total_covers
    plan.forecast_revenue = forecast_revenue.quantize(Decimal("0.01"))

    lines: list[dict[str, Any]] = []
    for ingredient_id, needed in sorted(requirement.items(), key=lambda kv: kv[1], reverse=True):
        ingredient = ingredients.get(ingredient_id)
        if ingredient is None:
            continue
        on_hand_qty = balances.get(ingredient_id, ZERO)

        # Prep quantity is the full forecast requirement grossed up for yield
        # loss, NOT the shortfall against stock. Raw stock in the walk-in is the
        # input to prep, not a substitute for it: 40 kg of onions in the chiller
        # is what you chop, not a reason to chop none. To end up with `needed`
        # usable, start with needed / yield.
        prep_qty = (
            (needed / ingredient.yield_pct) if ingredient.yield_pct > 0 else needed
        ).quantize(Decimal("0.0001"))

        # On-hand still matters, as a feasibility check: if there is not enough
        # raw material to do the prep, the kitchen needs to know this morning
        # rather than at 18:00.
        short_of_raw = prep_qty > on_hand_qty

        session.add(
            PrepPlanLine(
                plan_id=plan.id,
                ingredient_id=ingredient_id,
                forecast_usage=needed.quantize(Decimal("0.0001")),
                prep_quantity=prep_qty,
                uom=ingredient.base_uom,
                on_hand_at_plan=on_hand_qty.quantize(Decimal("0.0001")),
            )
        )
        if prep_qty > 0:
            note = ""
            if ingredient.yield_pct < 1:
                note = f"grossed up for {(1 - ingredient.yield_pct) * 100:.0f}% trim loss"
            if short_of_raw:
                gap = (prep_qty - on_hand_qty).quantize(Decimal("1"))
                note = f"SHORT {gap:.0f}{ingredient.base_uom} of raw stock" + (
                    f"; {note}" if note else ""
                )
            lines.append(
                {
                    "ingredient": ingredient.name,
                    "needed": f"{needed:.0f}{ingredient.base_uom}",
                    "on_hand": f"{on_hand_qty:.0f}{ingredient.base_uom}",
                    "prep": f"{prep_qty:.0f}{ingredient.base_uom}",
                    "short_of_raw": short_of_raw,
                    "yield_note": note,
                }
            )

    session.flush()
    publish(
        Event(
            Topic.PREP_PLAN_READY,
            {"business_date": target.isoformat(), "covers": result.total_covers},
            source_run_id=context.run_id,
        ),
        session=session,
    )

    top_items = sorted(
        ((items[i].name, q) for i, q in quantities.items()), key=lambda kv: kv[1], reverse=True
    )[:5]

    short_lines = [line for line in lines if line["short_of_raw"]]
    return {
        "business_date": target.isoformat(),
        "forecast_covers": result.total_covers,
        "forecast_revenue": str(plan.forecast_revenue),
        "ingredient_lines": len(lines),
        "short_of_raw_material": len(short_lines),
        "short_items": [line["ingredient"] for line in short_lines[:10]],
        "prep_value": str(cost_of_requirement(session, requirement)),
        "bias_applied": bool(bias),
        "top_items": [{"item": name, "forecast": str(qty)} for name, qty in top_items],
        "prep_list": lines[:30],
    }


def autonomous(context: ToolContext, perceived: dict[str, Any]) -> dict[str, Any]:
    return {
        "summary": (
            f"Forecasting {perceived.get('weekday')} {perceived.get('target_date')} from "
            f"{perceived.get('history_days')} days of history, after scoring yesterday's plan."
        ),
        "results": {},
        "tool_calls": [
            {"name": "score_yesterday", "args": {}},
            {"name": "build_prep_plan", "args": {"lookback_days": 56, "event_factor": "1.0"}},
        ],
    }


PREP_FORECASTER_AGENT = register(
    AgentSpec(
        name="prep_forecaster",
        department="kitchen",
        title="Dynamic Prep Forecaster",
        description=(
            "Predicts ingredient quantities to prep by analysing historical sales, seasonal "
            "trends and real-time demand, to minimise food waste."
        ),
        system_prompt=SYSTEM_PROMPT,
        model_tier="reasoning",
        tools=[
            ToolSpec(
                name="score_yesterday",
                description="Score yesterday's forecast against actual sales.",
                fn=score_yesterday,
                args_schema=ScoreArgs,
            ),
            ToolSpec(
                name="build_prep_plan",
                description=(
                    "Forecast per-item demand, explode through the recipe BOM to ingredient "
                    "quantities, and write today's prep list."
                ),
                fn=build_prep_plan,
                args_schema=ForecastArgs,
            ),
        ],
        perceive=perceive,
        autonomous=autonomous,
    )
)
