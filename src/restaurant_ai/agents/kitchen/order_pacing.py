"""Order Routing & Pacing Agent.

Turns open order lines into a firing plan for the kitchen display system:
which station cooks what, in what order, and at what moment.

The pacing rule is the one that matters. A table's plates have to land together,
so within a course the slowest dish sets the serve time and everything faster is
fired late enough to be ready at the same moment. Firing everything at once is
how food sits under the pass going cold.

It also protects channels from each other. A flood of delivery tickets must not
push a seated table's mains behind them, so dine-in takes priority within a
course, and any plate that still slips past its promised time is reported to the
pass rather than silently arriving late.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field
from sqlalchemy import select

from restaurant_ai import clock
from restaurant_ai.db.models import (
    KdsTicket,
    MenuItem,
    OrderChannel,
    OrderHeader,
    OrderLine,
    OrderStatus,
    Recipe,
    TicketStatus,
)
from restaurant_ai.domain.pacing import TicketRequest, plan_service, summarise_load
from restaurant_ai.events import Event, Topic, publish
from restaurant_ai.kernel.registry import register
from restaurant_ai.kernel.spec import AgentSpec, ToolContext, ToolSpec

SYSTEM_PROMPT = """You are the Order Routing and Pacing Agent for a restaurant kitchen.

You decide what each station cooks and when it fires.

The rules that matter:
- A table's plates land together. Within a course, the slowest dish sets the
  serve time; everything faster fires later so it is ready at the same moment.
- Courses are sequenced, not simultaneous. Starters go, then mains once the
  starters have been eaten.
- Stations have finite hands. When one is oversubscribed, say so plainly and
  name it as the bottleneck rather than quietly letting tickets pile up.
- Dine-in guests are sitting in the room watching. Delivery and takeaway can
  absorb a few minutes; a seated table cannot.

When you report, tell the pass what they need to act on: which station is
underwater, and which tables are going to get their food out of sync."""


class FireArgs(BaseModel):
    include_channels: str = Field("all", description="Comma-separated channels to fire, or 'all'.")


def _open_lines(session, business_date) -> list[tuple[OrderLine, OrderHeader, MenuItem]]:
    return list(
        session.execute(
            select(OrderLine, OrderHeader, MenuItem)
            .join(OrderHeader, OrderLine.order_id == OrderHeader.id)
            .join(MenuItem, OrderLine.menu_item_id == MenuItem.id)
            .where(
                OrderHeader.business_date == business_date,
                OrderHeader.status.in_([OrderStatus.OPEN, OrderStatus.FIRED]),
                OrderLine.is_voided.is_(False),
            )
        ).all()
    )


def perceive(context: ToolContext) -> dict[str, Any]:
    session = context.session
    rows = _open_lines(session, context.business_date)

    queued = session.execute(
        select(KdsTicket).where(
            KdsTicket.status.in_([TicketStatus.QUEUED, TicketStatus.IN_PROGRESS])
        )
    ).scalars()
    by_station: dict[str, int] = {}
    for ticket in queued:
        by_station[ticket.station.value] = by_station.get(ticket.station.value, 0) + 1

    return {
        "open_order_lines": len(rows),
        "open_orders": len({header.id for _line, header, _item in rows}),
        "tickets_in_flight": sum(by_station.values()),
        "station_queues": by_station,
        "channels": sorted({header.channel.value for _l, header, _i in rows}),
    }


def nothing_to_pace(context: dict[str, Any]) -> str | None:
    """Is the kitchen empty right now?

    This agent is scheduled every five minutes through service, and that is the
    right schedule: a ticket that lands at 19:03 has to reach the pass by 19:08,
    not at the top of the hour. But the schedule is sized to the *worst* case,
    and most of those wake-ups find no orders at all — a mamak between the lunch
    and dinner rushes, a restaurant whose till is not connected yet, or simply
    a quiet Tuesday.

    Reasoning about an empty kitchen produces the same answer every time and
    costs a model call to reach it: 156 a day, ahead of every other agent
    combined, and on a free tier the whole day's quota spent before the owner
    has asked a single question.

    Open lines and tickets in flight together are the whole of the work — the
    firing plan is computed from them and nothing else — so when both are zero
    there is genuinely nothing this run could decide.
    """
    if context.get("open_order_lines") or context.get("tickets_in_flight"):
        return None
    return "No open orders and nothing in flight — the kitchen is clear."


def fire_tickets(context: ToolContext, include_channels: str = "all") -> dict[str, Any]:
    """Route open order lines to stations and write the sequenced KDS tickets."""
    session = context.session
    rows = _open_lines(session, context.business_date)
    if not rows:
        return {"tickets": 0, "note": "No open order lines to fire."}

    wanted = (
        None
        if include_channels.strip().lower() == "all"
        else {c.strip() for c in include_channels.split(",") if c.strip()}
    )

    # Station comes from the recipe where one exists, falling back to the menu
    # item's own tag.
    recipes = {
        r.menu_item_id: r
        for r in session.execute(select(Recipe).where(Recipe.menu_item_id.isnot(None))).scalars()
    }

    # scalars() on a single-column select yields the values themselves, not
    # rows: these are order_line_id strings already.
    already = set(session.execute(select(KdsTicket.order_line_id)).scalars())

    requests: list[TicketRequest] = []
    for line, header, item in rows:
        if line.id in already:
            continue  # already fired; do not double-fire on a re-run
        if wanted is not None and header.channel.value not in wanted:
            continue
        recipe = recipes.get(item.id)
        requests.append(
            TicketRequest(
                order_id=header.id,
                order_line_id=line.id,
                order_number=header.order_number,
                menu_item_name=item.name,
                station=recipe.station if recipe else item.station,
                course=line.course or item.course,
                prep_seconds=recipe.prep_seconds if recipe else item.prep_seconds,
                quantity=int(line.quantity),
                channel=header.channel,
                placed_at=header.placed_at,
                modifiers=line.modifiers,
            )
        )

    if not requests:
        return {"tickets": 0, "note": "Every open line has already been fired."}

    plan = plan_service(requests, clock.now())

    for ticket in plan.tickets:
        session.add(
            KdsTicket(
                order_id=ticket.order_id,
                order_line_id=ticket.order_line_id,
                station=ticket.station,
                status=TicketStatus.QUEUED,
                course=ticket.course,
                sequence=ticket.sequence,
                fire_at=ticket.fire_at,
                estimated_seconds=ticket.estimated_seconds,
                modifiers=ticket.modifiers,
            )
        )

    fired_orders = {t.order_id for t in plan.tickets}
    for order_id in fired_orders:
        fired_header = session.get(OrderHeader, order_id)
        if fired_header is not None and fired_header.status == OrderStatus.OPEN:
            fired_header.status = OrderStatus.FIRED

    session.flush()
    publish(
        Event(
            Topic.KDS_TICKETS_FIRED,
            {"tickets": len(plan.tickets), "orders": len(fired_orders)},
            source_run_id=context.run_id,
        ),
        session=session,
    )

    slipped = [t for t in plan.tickets if t.slip_seconds >= 120]
    dine_in_slipped = [t for t in slipped if t.channel == OrderChannel.DINE_IN]

    return {
        "tickets": len(plan.tickets),
        "orders": len(fired_orders),
        "load": summarise_load(plan),
        "bottlenecks": [
            {
                "station": load.station.value,
                "tickets": load.ticket_count,
                "minutes": load.minutes,
                "peak_demand": load.peak_demand,
                "capacity": load.capacity,
                "held_back": load.delayed_count,
            }
            for load in plan.bottlenecks
        ],
        "slipped_tickets": len(slipped),
        "dine_in_slipped": len(dine_in_slipped),
        "warnings": plan.warnings,
        "next_fire": (min(t.fire_at for t in plan.tickets).isoformat() if plan.tickets else None),
    }


def autonomous(context: ToolContext, perceived: dict[str, Any]) -> dict[str, Any]:
    lines = perceived.get("open_order_lines", 0)
    if not lines:
        return {
            "summary": "No open orders. Kitchen is clear.",
            "results": {},
            "tool_calls": [],
        }
    return {
        "summary": (
            f"Routing {lines} open order line(s) across "
            f"{perceived.get('open_orders', 0)} order(s), sequencing courses to plate together."
        ),
        "results": {},
        "tool_calls": [{"name": "fire_tickets", "args": {"include_channels": "all"}}],
    }


ORDER_PACING_AGENT = register(
    AgentSpec(
        name="order_pacing",
        person="Ciknor",
        department="kitchen",
        title="Order Routing & Pacing Agent",
        description=(
            "Balances ticket queues across kitchen stations so courses fire in sequence and "
            "dine-in and takeaway orders do not bottleneck each other."
        ),
        system_prompt=SYSTEM_PROMPT,
        model_tier="conversational",
        tools=[
            ToolSpec(
                name="fire_tickets",
                description=(
                    "Route open order lines to stations, sequence them so each course plates "
                    "together, and write the KDS tickets."
                ),
                fn=fire_tickets,
                args_schema=FireArgs,
            )
        ],
        perceive=perceive,
        idle_when=nothing_to_pace,
        autonomous=autonomous,
    )
)
