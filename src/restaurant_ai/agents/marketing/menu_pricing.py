"""Dynamic Pricing & Menu Engineering Agent.

Classifies the menu by popularity against contribution margin, then proposes
bounded price and bundle changes.

Every price change is approval-gated and guardrailed: capped at a percentage per
change, rate-limited by a cooldown, floored on margin, and dropped entirely
unless the projected contribution gain beats the volume it costs. An agent with
unconstrained pricing authority is how a menu quietly doubles.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, Field

from restaurant_ai import clock
from restaurant_ai.agents.common import active_menu_items, units_sold_between
from restaurant_ai.config import get_settings
from restaurant_ai.db.models import MenuItem, MenuItemPriceHistory
from restaurant_ai.domain.costing import costed_menu_items, menu_item_cost
from restaurant_ai.domain.pricing import (
    ItemPerformance,
    classify_menu,
    propose_bundles,
    propose_price_changes,
)
from restaurant_ai.events import Event, Topic, publish
from restaurant_ai.kernel.registry import register
from restaurant_ai.kernel.spec import AgentSpec, ToolContext, ToolSpec

ZERO = Decimal("0")


class AnalyseArgs(BaseModel):
    window_days: int = Field(28, description="Trading window to analyse.")


class ProposeArgs(BaseModel):
    window_days: int = Field(28, description="Trading window to analyse.")
    max_proposals: int = Field(
        5,
        description=(
            "Most price changes to propose at once. Moving a lot of the menu in "
            "one go is what regulars notice."
        ),
    )


def _performances(session, business_date, window_days: int) -> list[ItemPerformance]:
    """Only dishes we can actually cost.

    A dish with no recipe costs zero, and zero cost is full margin — so left in,
    every uncosted dish would rank above every real one as a Star, and would
    drag the average margin up far enough to re-label honest dishes as
    Plowhorses. Excluding them is the difference between an analysis that is
    incomplete and one that is wrong; ``_uncosted`` reports how many, so
    incomplete is never silent.
    """
    start = business_date - timedelta(days=window_days)
    sold = units_sold_between(session, start, business_date)
    items = list(active_menu_items(session))
    costed = costed_menu_items(session, [item.id for item in items])
    return [
        ItemPerformance(
            menu_item_id=item.id,
            sku=item.sku,
            name=item.name,
            price=item.price,
            unit_cost=menu_item_cost(session, item.id),
            units_sold=sold.get(item.id, ZERO),
            last_price_change_on=item.last_price_change_on,
        )
        for item in items
        if item.id in costed
    ]


def _uncosted(session) -> list[str]:
    """Active dishes with no recipe — nothing can say what they earn."""
    items = list(active_menu_items(session))
    costed = costed_menu_items(session, [item.id for item in items])
    return [item.name for item in items if item.id not in costed]


def perceive(context: ToolContext) -> dict[str, Any]:
    session = context.session
    analysis = classify_menu(_performances(session, context.business_date, 28))
    uncosted = _uncosted(session)
    return {
        "items_analysed": len(analysis.items),
        "items_not_costed": len(uncosted),
        "not_costed_examples": uncosted[:5],
        "total_contribution": str(analysis.total_contribution),
        "average_margin": str(analysis.avg_margin),
        "class_counts": {
            menu_class.value: len(analysis.by_class(menu_class))
            for menu_class in {c.menu_class for c in analysis.items}
        },
    }


def analyse_menu(context: ToolContext, window_days: int = 28) -> dict[str, Any]:
    """Classify every item into its menu-engineering quadrant."""
    session = context.session
    analysis = classify_menu(_performances(session, context.business_date, window_days))
    if not analysis.items:
        return {"items": 0, "note": "No sales in the window; nothing to analyse."}

    for entry in analysis.items:
        item = session.get(MenuItem, entry.performance.menu_item_id)
        if item is not None:
            item.menu_class = entry.menu_class
    session.flush()

    return {
        "window_days": window_days,
        "items": len(analysis.items),
        "total_units": str(analysis.total_units),
        "total_contribution": str(analysis.total_contribution),
        "average_margin": str(analysis.avg_margin),
        "breakdown": [
            {
                "sku": entry.performance.sku,
                "name": entry.performance.name,
                "class": entry.menu_class.value,
                "price": str(entry.performance.price),
                "cost": str(entry.performance.unit_cost),
                "margin_pct": f"{entry.performance.margin_pct * 100:.1f}%",
                "units": str(entry.performance.units_sold),
                "popularity_index": str(entry.popularity_index),
                "contribution": str(entry.performance.total_contribution),
                "action": entry.recommendation,
            }
            for entry in analysis.items
        ],
    }


def propose_changes(
    context: ToolContext, window_days: int = 28, max_proposals: int = 5
) -> dict[str, Any]:
    """Propose bounded price changes and bundles. Approval-gated."""
    session = context.session
    settings = get_settings()
    analysis = classify_menu(_performances(session, context.business_date, window_days))
    if not analysis.items:
        return {"price_changes": 0, "bundles": 0, "note": "Nothing to analyse."}

    proposals = propose_price_changes(
        analysis,
        today=context.business_date,
        max_change_pct=settings.price_change_max_pct,
        cooldown_days=settings.price_change_cooldown_days,
        min_margin_pct=settings.min_gross_margin_pct,
        max_proposals=max_proposals,
    )
    bundles = propose_bundles(analysis, min_margin_pct=settings.min_gross_margin_pct)

    for proposal in proposals:
        publish(
            Event(
                Topic.PRICE_CHANGE_PROPOSED,
                {
                    "sku": proposal.sku,
                    "from": str(proposal.current_price),
                    "to": str(proposal.proposed_price),
                },
                source_run_id=context.run_id,
            ),
            session=session,
        )

    return {
        "price_changes": len(proposals),
        "bundles": len(bundles),
        "expected_gain": str(sum((p.expected_contribution_delta for p in proposals), ZERO)),
        "guardrails": {
            "max_change_pct": str(settings.price_change_max_pct),
            "cooldown_days": settings.price_change_cooldown_days,
            "min_margin_pct": str(settings.min_gross_margin_pct),
        },
        "proposals": [
            {
                "menu_item_id": p.menu_item_id,
                "sku": p.sku,
                "name": p.name,
                "current_price": str(p.current_price),
                "proposed_price": str(p.proposed_price),
                "change_pct": f"{p.change_pct * 100:+.1f}%",
                "class": p.menu_class.value,
                "expected_gain": str(p.expected_contribution_delta),
                "rationale": p.rationale,
            }
            for p in proposals
        ],
        "bundle_proposals": [
            {
                "name": b.name,
                "list_price": str(b.list_price),
                "bundle_price": str(b.bundle_price),
                "margin_pct": f"{b.bundle_margin_pct * 100:.1f}%",
                "rationale": b.rationale,
            }
            for b in bundles
        ],
    }


def commit_price_changes(context: ToolContext, payload: dict[str, Any]) -> dict[str, Any]:
    """Approved: apply the new prices and log the change."""
    session = context.session
    applied: list[str] = []

    for proposal in payload.get("proposals", []):
        item = session.get(MenuItem, proposal["menu_item_id"])
        if item is None:
            continue
        old_price = item.price
        new_price = Decimal(proposal["proposed_price"])

        session.add(
            MenuItemPriceHistory(
                menu_item_id=item.id,
                old_price=old_price,
                new_price=new_price,
                effective_from=clock.utcnow(),
                reason=proposal["rationale"],
                changed_by=payload.get("approved_by") or "menu_pricing_agent",
            )
        )
        item.price = new_price
        item.last_price_change_on = context.business_date
        applied.append(f"{item.sku}: {old_price} -> {new_price}")

        publish(
            Event(
                Topic.PRICE_CHANGED,
                {"sku": item.sku, "from": str(old_price), "to": str(new_price)},
                source_run_id=context.run_id,
            ),
            session=session,
        )

    session.flush()
    return {"applied": applied, "count": len(applied)}


def autonomous(context: ToolContext, perceived: dict[str, Any]) -> dict[str, Any]:
    return {
        "summary": (
            f"Analysing {perceived.get('items_analysed', 0)} menu items by popularity and "
            f"contribution margin, then proposing guardrailed price changes."
        ),
        "results": {},
        "tool_calls": [
            {"name": "analyse_menu", "args": {"window_days": 28}},
            {"name": "propose_changes", "args": {"window_days": 28, "max_proposals": 5}},
        ],
    }


_price_tool = ToolSpec(
    name="propose_changes",
    description=(
        "Propose price and bundle changes within the configured guardrails. "
        "Requires human approval before any price moves."
    ),
    fn=propose_changes,
    args_schema=ProposeArgs,
    requires_approval=True,
    # Bundles are changes to what the restaurant charges, and gating only on
    # price_changes let three of them through unapproved on the first live run:
    # the model proposed no price moves and three bundles, and the run reported
    # "completed" without ever asking anyone.
    gate_when=lambda r: r.get("price_changes", 0) > 0 or r.get("bundles", 0) > 0,
    approval_value=lambda r: Decimal(str(r.get("expected_gain", "0"))),
    approval_summary=lambda r: (
        f"{r['price_changes']} price change(s) proposed, expected contribution gain "
        f"{r['expected_gain']} over the window"
    ),
    approval_detail=lambda r: (
        "\n\n".join(
            f"    {p['name']} ({p['class']}): {p['current_price']} -> {p['proposed_price']} "
            f"({p['change_pct']})\n    {p['rationale']}"
            for p in r.get("proposals", [])
        )
        or "No changes proposed."
    ),
)
_price_tool.commit_fn = commit_price_changes  # type: ignore[attr-defined]


MENU_PRICING_AGENT = register(
    AgentSpec(
        name="menu_pricing",
        person="Irma",
        department="marketing",
        title="Dynamic Pricing & Menu Engineering Agent",
        description=(
            "Analyses item-level margins, tracks bestsellers against slow-moving stock, and "
            "tests price elasticities and bundle offers."
        ),
        system_prompt=(
            "You are the Dynamic Pricing and Menu Engineering Agent for a restaurant.\n\n"
            "You classify the menu on two axes - how often something sells, and what it earns "
            "when it does - and act on the four quadrants that produces.\n\n"
            "- Stars sell well and earn well. Protect them. Do not discount them.\n"
            "- Plowhorses sell well and earn little. Demand is proven, so a modest price rise "
            "is the lower-risk lever, or re-engineer the plate cost.\n"
            "- Puzzles earn well but few order them. Fix the menu position and the description "
            "before you touch the price.\n"
            "- Dogs do neither. Bundle them to move the stock they consume, or delist them.\n\n"
            "Constraints you do not get to override: a cap on how far any price moves, a "
            "cooldown between changes to the same item, and a margin floor. A price rise that "
            "loses more volume than it gains in margin is not an improvement - check the "
            "arithmetic before proposing it.\n\n"
            "You propose; a human decides."
        ),
        model_tier="reasoning",
        tools=[
            ToolSpec(
                name="analyse_menu",
                description="Classify every menu item by popularity and contribution margin.",
                fn=analyse_menu,
                args_schema=AnalyseArgs,
            ),
            _price_tool,
        ],
        perceive=perceive,
        autonomous=autonomous,
    )
)
