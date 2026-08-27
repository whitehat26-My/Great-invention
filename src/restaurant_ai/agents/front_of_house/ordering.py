"""Conversational Order Agent.

Takes orders from phone, drive-thru and digital kiosks, interprets custom
dietary requests, and routes them to the POS.

The dietary handling is the part that has to be right. "No belacan - shellfish
allergy" cannot be answered from a hand-maintained allergen label, because
belacan is buried two levels down inside the sambal that sits inside the nasi
lemak. The agent walks the actual recipe BOM, so it can say which ingredient
carries the allergen and where it came from, and it refuses rather than guesses
when a dish cannot be made safe.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from pydantic import BaseModel, Field
from sqlalchemy import select

from restaurant_ai import clock
from restaurant_ai.agents.common import active_menu_items
from restaurant_ai.db.models import (
    MenuItem,
    MenuSection,
    OrderChannel,
    OrderHeader,
    OrderLine,
    OrderStatus,
)
from restaurant_ai.domain.costing import ingredients_carrying, menu_item_allergens
from restaurant_ai.events import Event, Topic, publish
from restaurant_ai.kernel.registry import register
from restaurant_ai.kernel.spec import AgentSpec, ToolContext, ToolSpec

ZERO = Decimal("0")

# Requests the kitchen can honour by omitting or substituting a component.
ADAPTABLE_GARNISHES = {"peanut", "sesame", "treenut"}

SYSTEM_PROMPT = """You are the Conversational Order Agent for a restaurant.

You take orders by phone, at the drive-thru and on the kiosks, and you send them
to the POS.

On dietary requests, which are the part that matters most:
- Check the actual recipe, not the dish name. Sambal contains belacan, which is
  shrimp paste, so anything made with sambal is not shellfish-safe however
  vegetarian it sounds.
- Say which ingredient is the problem, not just that there is one. "The sambal
  contains belacan (shrimp paste)" lets a guest decide; "contains shellfish"
  does not.
- If a dish can be made safe by leaving something off, offer that. If it cannot,
  say so plainly and suggest something that works.
- Never guess. An allergy you are unsure about is one you escalate.

Confirm the order back before sending it: items, quantities, modifications and
total."""


class MenuLookupArgs(BaseModel):
    query: str = Field("", description="Filter by name or section; empty lists everything.")


class AllergenCheckArgs(BaseModel):
    sku: str = Field(..., description="Menu item SKU to check.")
    allergens: str = Field(..., description="Comma-separated allergens to check for.")


class PlaceOrderArgs(BaseModel):
    channel: str = Field("phone", description="phone | drive_thru | kiosk | takeaway")
    items: str = Field(..., description="Comma-separated SKU:quantity pairs, e.g. 'MNU-KOPIO:2'.")
    party_size: int = Field(1)
    guest_phone: str | None = None
    notes: str | None = None


def perceive(context: ToolContext) -> dict[str, Any]:
    session = context.session
    items = active_menu_items(session)
    open_orders = session.execute(
        select(OrderHeader).where(
            OrderHeader.business_date == context.business_date,
            OrderHeader.status == OrderStatus.OPEN,
        )
    ).scalars()
    return {
        "menu_items_available": len(items),
        "open_orders": len(list(open_orders)),
        "channels": ["phone", "drive_thru", "kiosk", "takeaway"],
    }


def lookup_menu(context: ToolContext, query: str = "") -> dict[str, Any]:
    """The live menu, with derived allergen sets so nothing is stated from memory."""
    session = context.session
    stmt = (
        select(MenuItem, MenuSection)
        .join(MenuSection, MenuItem.section_id == MenuSection.id)
        .where(MenuItem.is_active)
    )
    rows = list(session.execute(stmt).all())

    needle = query.strip().lower()
    results = []
    for item, section in rows:
        if needle and needle not in item.name.lower() and needle not in section.name.lower():
            continue
        results.append(
            {
                "sku": item.sku,
                "name": item.name,
                "section": section.name,
                "price": str(item.price),
                "description": item.description,
                "allergens": sorted(menu_item_allergens(session, item.id)),
            }
        )
    return {"count": len(results), "items": results}


def check_allergens(context: ToolContext, sku: str, allergens: str) -> dict[str, Any]:
    """Check a dish against specific allergens by walking its full recipe.

    Answers with the offending ingredient rather than a bare yes/no, because a
    guest deciding whether to risk a dish needs to know what is in it.
    """
    session = context.session
    item = session.execute(select(MenuItem).where(MenuItem.sku == sku)).scalar_one_or_none()
    if item is None:
        return {"safe": False, "error": f"No menu item with SKU {sku!r}."}

    requested = {a.strip().lower() for a in allergens.split(",") if a.strip()}
    if not requested:
        return {"safe": True, "note": "No allergens specified."}

    carriers = ingredients_carrying(session, item.id, requested)
    if not carriers:
        return {
            "safe": True,
            "sku": sku,
            "item": item.name,
            "checked": sorted(requested),
            "note": f"{item.name} contains none of: {', '.join(sorted(requested))}.",
        }

    # Some allergens ride on a garnish and can simply be left off; others are
    # cooked into a base and cannot.
    removable = {a: names for a, names in carriers.items() if a in ADAPTABLE_GARNISHES}
    blocking = {a: names for a, names in carriers.items() if a not in ADAPTABLE_GARNISHES}

    explanation = "; ".join(
        f"{allergen} is in {', '.join(names)}" for allergen, names in carriers.items()
    )
    return {
        "safe": False,
        "sku": sku,
        "item": item.name,
        "checked": sorted(requested),
        "carriers": carriers,
        "can_be_adapted": bool(removable) and not blocking,
        "adaptation": (
            f"Can be prepared without {', '.join(n for names in removable.values() for n in names)}."
            if removable and not blocking
            else None
        ),
        "explanation": f"{item.name}: {explanation}.",
        "recommendation": (
            f"Prepare without {', '.join(n for names in removable.values() for n in names)}."
            if removable and not blocking
            else f"Cannot be made safe: {explanation}. Offer an alternative."
        ),
    }


def suggest_alternatives(context: ToolContext, sku: str, allergens: str) -> dict[str, Any]:
    """Find dishes that are actually safe for these allergens."""
    session = context.session
    requested = {a.strip().lower() for a in allergens.split(",") if a.strip()}
    safe = []
    for item in active_menu_items(session):
        if item.sku == sku:
            continue
        if not (menu_item_allergens(session, item.id) & requested):
            safe.append({"sku": item.sku, "name": item.name, "price": str(item.price)})
    return {"count": len(safe), "alternatives": safe[:8], "avoiding": sorted(requested)}


def place_order(
    context: ToolContext,
    channel: str = "phone",
    items: str = "",
    party_size: int = 1,
    guest_phone: str | None = None,
    notes: str | None = None,
) -> dict[str, Any]:
    """Price an order, write it, and push it to the POS."""
    from restaurant_ai.integrations import get_integrations
    from restaurant_ai.integrations.base import PosOrder, PosOrderLine

    session = context.session
    parsed: list[tuple[str, int]] = []
    for chunk in items.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        sku, _, raw_quantity = chunk.partition(":")
        try:
            parsed.append((sku.strip(), max(1, int(raw_quantity or 1))))
        except ValueError:
            return {"placed": False, "error": f"Could not read quantity in {chunk!r}."}

    if not parsed:
        return {"placed": False, "error": "No items given."}

    menu = {
        item.sku: item
        for item in session.execute(
            select(MenuItem).where(MenuItem.sku.in_([sku for sku, _ in parsed]))
        ).scalars()
    }
    missing = [sku for sku, _ in parsed if sku not in menu]
    if missing:
        return {"placed": False, "error": f"Unknown SKU(s): {', '.join(missing)}."}

    now = clock.now()
    order = OrderHeader(
        order_number=_next_order_number(session, context.business_date),
        channel=OrderChannel(channel),
        status=OrderStatus.OPEN,
        party_size=party_size,
        placed_at=now,
        business_date=context.business_date,
        notes=notes,
    )
    session.add(order)
    session.flush()

    subtotal = ZERO
    for sku, quantity in parsed:
        item = menu[sku]
        line_total = (item.price * quantity).quantize(Decimal("0.01"))
        session.add(
            OrderLine(
                order_id=order.id,
                menu_item_id=item.id,
                quantity=Decimal(quantity),
                unit_price=item.price,
                line_total=line_total,
                course=item.course,
                modifiers=notes,
            )
        )
        subtotal += line_total

    tax = (subtotal * Decimal("0.06")).quantize(Decimal("0.01"))
    order.subtotal = subtotal
    order.tax = tax
    order.total = subtotal + tax
    session.flush()

    external_ref = get_integrations().pos.push_order(
        PosOrder(
            external_id=order.order_number,
            channel=channel,
            placed_at=now,
            lines=[
                PosOrderLine(
                    sku=sku,
                    quantity=quantity,
                    unit_price=menu[sku].price,
                    modifiers=notes,
                    course=menu[sku].course,
                )
                for sku, quantity in parsed
            ],
            party_size=party_size,
            guest_phone=guest_phone,
            notes=notes,
        )
    )
    order.external_ref = external_ref
    session.flush()

    publish(
        Event(
            Topic.ORDER_PLACED,
            {"order_number": order.order_number, "total": str(order.total), "channel": channel},
            source_run_id=context.run_id,
        ),
        session=session,
    )

    return {
        "placed": True,
        "order_number": order.order_number,
        "external_ref": external_ref,
        "channel": channel,
        "subtotal": str(subtotal),
        "tax": str(tax),
        "total": str(order.total),
        "lines": [
            {"sku": sku, "name": menu[sku].name, "quantity": quantity} for sku, quantity in parsed
        ],
    }


def _next_order_number(session, business_date) -> str:
    from sqlalchemy import func

    prefix = f"C{business_date.strftime('%y%m%d')}"
    count = session.execute(
        select(func.count(OrderHeader.id)).where(OrderHeader.order_number.like(f"{prefix}%"))
    ).scalar_one()
    return f"{prefix}-{count + 1:05d}"


def autonomous(context: ToolContext, perceived: dict[str, Any]) -> dict[str, Any]:
    """Standing by: this agent is driven by inbound guest contact, not a schedule."""
    return {
        "summary": (
            f"Ready to take orders across {', '.join(perceived.get('channels', []))} "
            f"against {perceived.get('menu_items_available', 0)} available menu items."
        ),
        "results": {},
        "tool_calls": [],
    }


ORDER_AGENT = register(
    AgentSpec(
        name="ordering",
        department="front_of_house",
        title="Conversational Order Agent",
        description=(
            "Takes phone, drive-thru and digital kiosk orders, interprets custom dietary "
            "requests against the recipe, and routes orders to the POS."
        ),
        system_prompt=SYSTEM_PROMPT,
        model_tier="conversational",
        tools=[
            ToolSpec(
                name="lookup_menu",
                description="List the live menu with prices and derived allergens.",
                fn=lookup_menu,
                args_schema=MenuLookupArgs,
            ),
            ToolSpec(
                name="check_allergens",
                description=(
                    "Check a dish against specific allergens by walking its full recipe, "
                    "naming the ingredient that carries each one."
                ),
                fn=check_allergens,
                args_schema=AllergenCheckArgs,
            ),
            ToolSpec(
                name="suggest_alternatives",
                description="Find dishes free of the given allergens.",
                fn=suggest_alternatives,
                args_schema=AllergenCheckArgs,
            ),
            ToolSpec(
                name="place_order",
                description="Price the order, record it, and push it to the POS.",
                fn=place_order,
                args_schema=PlaceOrderArgs,
            ),
        ],
        perceive=perceive,
        autonomous=autonomous,
    )
)
