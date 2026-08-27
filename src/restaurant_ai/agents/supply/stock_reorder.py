"""Stock Tracking & Auto-Reorder Agent.

Watches ingredient levels against reorder points and drafts purchase orders to
suppliers when they trip.

Every purchase order is approval-gated. The agent computes the shortfall, picks
the pack, respects the supplier's minimum order quantity and delivery days, and
writes the order as a DRAFT — then stops. Nothing is sent to a supplier until a
human approves it in Slack or Telegram. Approval flips the order to SENT and
transmits it.

Reorder points are recalculated from observed usage rather than being static:
demand drifts with the season and the menu, and a reorder point set once is
wrong within a month.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from pydantic import BaseModel, Field
from sqlalchemy import select

from restaurant_ai import clock
from restaurant_ai.agents.common import (
    daily_ingredient_usage,
    next_po_number,
    on_hand_all,
    on_order_all,
    preferred_stock_items,
)
from restaurant_ai.config import get_settings
from restaurant_ai.db.models import (
    Ingredient,
    PurchaseOrder,
    PurchaseOrderLine,
    PurchaseOrderStatus,
    ReorderPolicy,
)
from restaurant_ai.domain.inventory import (
    StockPosition,
    SupplierPack,
    build_purchase_orders,
    evaluate_position,
    recalculate_usage,
    reorder_point,
    safety_stock,
)
from restaurant_ai.events import Event, Topic, publish
from restaurant_ai.kernel.registry import register
from restaurant_ai.kernel.spec import AgentSpec, ToolContext, ToolSpec

ZERO = Decimal("0")

SYSTEM_PROMPT = """You are the Stock Tracking and Auto-Reorder Agent for a restaurant.

Your job is to keep the kitchen in stock without tying up cash or filling the
walk-in with food that will spoil. You watch ingredient levels against reorder
points and draft purchase orders when they trip.

Principles:
- Never let a headline dish go out of stock. A stockout on nasi lemak costs far
  more than carrying an extra day of chicken.
- Never order more shelf life than an ingredient has. Fresh produce ordered
  seven days deep goes in the bin, not on the plate.
- Consolidate by supplier and respect their minimum order values and delivery
  days. An order the supplier will not accept is not an order.
- Explain every line in terms a chef would recognise: what is on hand, how many
  days that covers, and what you are ordering to reach.

You draft; a human approves. Never imply an order has been sent."""


class RecalculatePolicyArgs(BaseModel):
    lookback_days: int = Field(28, description="Days of usage history to learn from.")


class DraftPurchaseOrdersArgs(BaseModel):
    reason: str = Field("Scheduled reorder sweep", description="Why this sweep ran.")


def perceive(context: ToolContext) -> dict[str, Any]:
    """Current stock position for every ingredient, against its reorder point."""
    session = context.session
    balances = on_hand_all(session)
    inbound = on_order_all(session)
    packs = preferred_stock_items(session)
    settings = get_settings()

    ingredients = {i.id: i for i in session.execute(select(Ingredient)).scalars()}
    policies = {p.ingredient_id: p for p in session.execute(select(ReorderPolicy)).scalars()}

    below: list[dict[str, Any]] = []
    for ingredient_id, ingredient in ingredients.items():
        policy = policies.get(ingredient_id)
        if policy is None or policy.avg_daily_usage <= 0:
            continue
        entry = packs.get(ingredient_id)
        lead = entry[1].lead_time_days if entry else 2
        rop = reorder_point(
            policy.avg_daily_usage, lead, policy.usage_stddev, settings.service_level_z
        )
        available = balances.get(ingredient_id, ZERO) + inbound.get(ingredient_id, ZERO)
        if available <= rop:
            below.append(
                {
                    "ingredient": ingredient.name,
                    "code": ingredient.code,
                    "on_hand": str(balances.get(ingredient_id, ZERO).quantize(Decimal("0.01"))),
                    "on_order": str(inbound.get(ingredient_id, ZERO).quantize(Decimal("0.01"))),
                    "reorder_point": str(rop.quantize(Decimal("0.01"))),
                    "days_cover": str(
                        (balances.get(ingredient_id, ZERO) / policy.avg_daily_usage).quantize(
                            Decimal("0.1")
                        )
                    ),
                }
            )

    return {
        "ingredients_tracked": len(ingredients),
        "policies_configured": len(policies),
        "below_reorder_point": len(below),
        "items": sorted(below, key=lambda d: Decimal(d["days_cover"]))[:25],
    }


def recalculate_policies(context: ToolContext, lookback_days: int = 28) -> dict[str, Any]:
    """Refresh average usage and its variability from what actually got consumed."""
    session = context.session
    usage = daily_ingredient_usage(session, lookback_days, until=context.business_date)
    settings = get_settings()
    packs = preferred_stock_items(session)

    updated = 0
    for ingredient_id, series in usage.items():
        if not series:
            continue
        mean, sigma = recalculate_usage(series)
        entry = packs.get(ingredient_id)
        lead = entry[1].lead_time_days if entry else 2

        policy = session.execute(
            select(ReorderPolicy).where(ReorderPolicy.ingredient_id == ingredient_id)
        ).scalar_one_or_none()
        if policy is None:
            policy = ReorderPolicy(ingredient_id=ingredient_id)
            session.add(policy)

        policy.avg_daily_usage = mean
        policy.usage_stddev = sigma
        policy.safety_stock = safety_stock(sigma, lead, settings.service_level_z)
        policy.reorder_point = reorder_point(mean, lead, sigma, settings.service_level_z)
        policy.last_recalculated_at = clock.utcnow()
        updated += 1

    session.flush()
    return {
        "policies_updated": updated,
        "lookback_days": lookback_days,
        "note": f"Reorder points refreshed from {lookback_days} days of observed usage.",
    }


def draft_purchase_orders(
    context: ToolContext, reason: str = "Scheduled reorder sweep"
) -> dict[str, Any]:
    """Draft one purchase order per supplier for everything below its reorder point.

    Writes the orders as DRAFT and returns them for approval. Nothing reaches a
    supplier from here.
    """
    session = context.session
    settings = get_settings()

    balances = on_hand_all(session)
    inbound = on_order_all(session)
    packs = preferred_stock_items(session)
    ingredients = {i.id: i for i in session.execute(select(Ingredient)).scalars()}
    policies = {p.ingredient_id: p for p in session.execute(select(ReorderPolicy)).scalars()}

    suggestions = []
    for ingredient_id, ingredient in ingredients.items():
        policy = policies.get(ingredient_id)
        entry = packs.get(ingredient_id)
        if policy is None or entry is None or policy.avg_daily_usage <= 0:
            continue
        stock_item, supplier = entry

        position = StockPosition(
            ingredient_id=ingredient_id,
            code=ingredient.code,
            name=ingredient.name,
            base_uom=ingredient.base_uom,
            on_hand=balances.get(ingredient_id, ZERO),
            on_order=inbound.get(ingredient_id, ZERO),
            avg_daily_usage=policy.avg_daily_usage,
            usage_stddev=policy.usage_stddev,
            lead_time_days=supplier.lead_time_days,
            target_days_cover=policy.target_days_cover,
            shelf_life_days=ingredient.shelf_life_days,
        )
        pack = SupplierPack(
            stock_item_id=stock_item.id,
            supplier_id=supplier.id,
            supplier_code=supplier.code,
            supplier_name=supplier.name,
            supplier_sku=stock_item.supplier_sku,
            pack_size=stock_item.pack_size,
            pack_uom=stock_item.pack_uom,
            contract_price=stock_item.contract_price,
            min_order_qty=stock_item.min_order_qty,
            lead_time_days=supplier.lead_time_days,
            min_order_value=supplier.min_order_value,
            delivery_days=tuple(
                int(d) for d in (supplier.delivery_days or "").split(",") if d.strip().isdigit()
            ),
        )
        suggestion = evaluate_position(position, pack, settings.service_level_z)
        if suggestion is not None:
            suggestions.append(suggestion)

    if not suggestions:
        return {
            "drafted": 0,
            "total_value": "0.00",
            "orders": [],
            "note": "Every ingredient is above its reorder point. Nothing to order.",
        }

    drafts = build_purchase_orders(suggestions, context.business_date)
    written: list[dict[str, Any]] = []

    for draft in drafts:
        po_number = next_po_number(session, context.business_date)
        order = PurchaseOrder(
            po_number=po_number,
            supplier_id=draft.supplier_id,
            status=PurchaseOrderStatus.PENDING_APPROVAL,
            expected_delivery_on=draft.expected_delivery_on,
            subtotal=draft.subtotal,
            tax=ZERO,
            total=draft.total,
            rationale=" ".join([reason, *draft.notes]),
            created_by_run_id=context.run_id,
        )
        session.add(order)
        session.flush()

        for line in draft.lines:
            session.add(
                PurchaseOrderLine(
                    purchase_order_id=order.id,
                    stock_item_id=line.pack.stock_item_id,
                    quantity_packs=line.packs_to_order,
                    unit_price=line.pack.contract_price,
                    line_total=line.line_cost,
                )
            )

        publish(
            Event(
                Topic.PURCHASE_ORDER_DRAFTED,
                {"po_number": po_number, "supplier": draft.supplier_name, "total": draft.total},
                source_run_id=context.run_id,
            ),
            session=session,
        )

        written.append(
            {
                "po_number": po_number,
                "purchase_order_id": order.id,
                "supplier": draft.supplier_name,
                "supplier_code": draft.supplier_code,
                "expected_delivery_on": draft.expected_delivery_on.isoformat(),
                "subtotal": str(draft.subtotal),
                "below_minimum": draft.below_minimum,
                "notes": draft.notes,
                "lines": [
                    {
                        "ingredient": line.name,
                        "packs": str(line.packs_to_order),
                        "pack_size": f"{line.pack.pack_size:.0f}",
                        "unit_price": str(line.pack.contract_price),
                        "line_cost": str(line.line_cost),
                        "urgency": line.urgency,
                        "rationale": line.rationale,
                    }
                    for line in draft.lines
                ],
            }
        )

    session.flush()
    total = sum((Decimal(o["subtotal"]) for o in written), ZERO)
    return {
        "drafted": len(written),
        "total_value": str(total),
        "orders": written,
        "urgent_lines": sum(
            1 for o in written for line in o["lines"] if line["urgency"] in ("stockout", "critical")
        ),
    }


def commit_purchase_orders(context: ToolContext, payload: dict[str, Any]) -> dict[str, Any]:
    """Approved: send the orders to their suppliers and mark them SENT."""
    from restaurant_ai.integrations import get_integrations

    session = context.session
    supplier_port = get_integrations().supplier
    sent: list[str] = []

    for order_payload in payload.get("orders", []):
        order = session.get(PurchaseOrder, order_payload["purchase_order_id"])
        if order is None:
            continue

        lines = list(
            session.execute(
                select(PurchaseOrderLine).where(PurchaseOrderLine.purchase_order_id == order.id)
            ).scalars()
        )
        transmit = [(line.stock_item.supplier_sku, line.quantity_packs) for line in lines]
        prices = {line.stock_item.supplier_sku: line.unit_price for line in lines}

        supplier_port.send_purchase_order(
            order.po_number, order_payload["supplier_code"], transmit, prices
        )

        order.status = PurchaseOrderStatus.SENT
        order.approved_at = clock.utcnow()
        order.approved_by = payload.get("approved_by") or "approver"
        sent.append(order.po_number)

        publish(
            Event(
                Topic.PURCHASE_ORDER_SENT,
                {"po_number": order.po_number, "total": str(order.total)},
                source_run_id=context.run_id,
            ),
            session=session,
        )

    session.flush()
    return {
        "sent": sent,
        "count": len(sent),
        "note": f"{len(sent)} purchase order(s) transmitted to suppliers.",
    }


def autonomous(context: ToolContext, perceived: dict[str, Any]) -> dict[str, Any]:
    """Deterministic path: refresh policies, then draft whatever is short.

    Both tools always run, and in this order. The draft step must not be
    conditioned on what ``perceive`` saw: perceive reads the *existing* reorder
    points, and recalculating them is the first thing this run does, so that
    snapshot is stale by the time the decision is taken. An ingredient can sit
    comfortably above a reorder point computed from last month's demand and well
    below the one computed from this week's.

    draft_purchase_orders re-evaluates every position against the refreshed
    policies and drafts nothing when nothing is short, so running it
    unconditionally is both correct and cheap.
    """
    below = perceived.get("below_reorder_point", 0)
    calls: list[dict[str, Any]] = [
        {"name": "recalculate_policies", "args": {"lookback_days": 28}},
        {
            "name": "draft_purchase_orders",
            "args": {
                "reason": (
                    "Reorder sweep against freshly recalculated reorder points "
                    f"({below} ingredient(s) were already short on the previous policy)."
                )
            },
        },
    ]
    return {
        "summary": (
            "Refreshing reorder policies from recent usage, then drafting purchase orders "
            "for anything at or below the updated reorder points."
        ),
        "results": {},
        "tool_calls": calls,
    }


_draft_tool = ToolSpec(
    name="draft_purchase_orders",
    description=(
        "Draft purchase orders for every ingredient at or below its reorder point, "
        "grouped by supplier. Requires human approval before anything is sent."
    ),
    fn=draft_purchase_orders,
    args_schema=DraftPurchaseOrdersArgs,
    requires_approval=True,
    gate_when=lambda r: r.get("drafted", 0) > 0,
    approval_value=lambda r: Decimal(str(r.get("total_value", "0"))),
    approval_summary=lambda r: (
        f"{r['drafted']} purchase order(s) totalling {r['total_value']}"
        + (f", {r['urgent_lines']} urgent line(s)" if r.get("urgent_lines") else "")
    ),
    approval_detail=lambda r: (
        "\n".join(
            f"{o['supplier']} ({o['po_number']}) - {o['subtotal']}, deliver {o['expected_delivery_on']}\n"
            + "\n".join(f"    - {line['rationale']}" for line in o["lines"])
            for o in r.get("orders", [])
        )
        or "No orders drafted."
    ),
)
_draft_tool.commit_fn = commit_purchase_orders  # type: ignore[attr-defined]


STOCK_REORDER_AGENT = register(
    AgentSpec(
        name="stock_reorder",
        department="supply",
        title="Stock Tracking & Auto-Reorder Agent",
        description=(
            "Monitors real-time ingredient levels via POS-driven deductions and drafts "
            "purchase orders when stock hits its reorder threshold."
        ),
        system_prompt=SYSTEM_PROMPT,
        model_tier="reasoning",
        tools=[
            ToolSpec(
                name="recalculate_policies",
                description=(
                    "Recalculate average daily usage, variability, safety stock and reorder "
                    "points from recent consumption."
                ),
                fn=recalculate_policies,
                args_schema=RecalculatePolicyArgs,
            ),
            _draft_tool,
        ],
        perceive=perceive,
        autonomous=autonomous,
    )
)
