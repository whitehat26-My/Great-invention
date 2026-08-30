"""What is real, and what is still a demonstration.

The platform is finished long before the restaurant is in it. Every agent runs,
every test passes, the dashboard draws — on invented data. That is the right way
to build it and a dangerous way to leave it, because a system reporting
confidently on fiction looks exactly like one reporting on fact.

This answers, per agent, the only question that matters before anyone acts on a
number: **can this agent tell me the truth yet, and if not, what is missing?**

Two things it deliberately does not do. It does not score the restaurant — an
agent with nothing to work on is not failing, it is waiting. And it never says
"add data": every gap names the specific thing and the command or the act that
supplies it, because "insufficient data" is where most of these projects stop.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from restaurant_ai import demo
from restaurant_ai.db import models as m

READY = "ready"
PARTIAL = "partial"
WAITING = "waiting"


@dataclass
class Readiness:
    """One agent, and whether its answers mean anything yet."""

    agent: str
    person: str
    state: str
    detail: str
    fix: str = ""

    @property
    def ok(self) -> bool:
        return self.state == READY


@dataclass
class Picture:
    agents: list[Readiness] = field(default_factory=list)
    facts: dict[str, Any] = field(default_factory=dict)

    @property
    def ready(self) -> list[Readiness]:
        return [a for a in self.agents if a.state == READY]

    @property
    def waiting(self) -> list[Readiness]:
        return [a for a in self.agents if a.state == WAITING]


def _count(session: Session, model: Any) -> int:
    return int(session.execute(select(func.count()).select_from(model)).scalar_one())


def survey(session: Session) -> dict[str, Any]:
    """The counts every judgement below is made from."""
    dishes = _count(session, m.MenuItem)
    # A recipe with no components in it, which is what the importer writes for a
    # dish whose BOM sheet is empty. Counting recipe rows says every dish is
    # costed the moment the menu is imported — the exact mistake this module
    # exists to catch, made here first.
    costed = int(
        session.execute(
            select(func.count(func.distinct(m.Recipe.menu_item_id)))
            .select_from(m.Recipe)
            .join(m.RecipeComponent, m.RecipeComponent.recipe_id == m.Recipe.id)
        ).scalar_one()
    )
    return {
        "dishes": dishes,
        "costed_dishes": costed,
        # The gap that quietly inflates every margin: a dish with no recipe
        # costs nothing, so it earns 100% and the average is a fiction.
        "uncosted_dishes": max(0, dishes - costed),
        "ingredients": _count(session, m.Ingredient),
        "stock_items": _count(session, m.StockItem),
        "reorder_policies": _count(session, m.ReorderPolicy),
        "suppliers": _count(session, m.Supplier),
        "staff": _count(session, m.Staff),
        "availability": _count(session, m.Availability),
        "sops": _count(session, m.SopDocument),
        "reviews": _count(session, m.Review),
        "real_orders": demo.real_orders(session),
        "demo_orders": demo.synthetic_orders(session),
    }


def _needs(state: str, detail: str, fix: str = "") -> tuple[str, str, str]:
    return state, detail, fix


def look(session: Session) -> Picture:
    """Every agent, and what stands between it and a true answer."""
    from restaurant_ai.kernel.registry import all_agents

    f = survey(session)
    picture = Picture(facts=f)

    sales = f["real_orders"] > 0
    no_sales = _needs(
        WAITING,
        "no real sales recorded yet",
        "Send `/sold 20 nasi lemak, 35 teh tarik` to the bot after a service, or connect "
        "the POS to POST /webhooks/pos.",
    )
    costs = f["dishes"] > 0 and f["uncosted_dishes"] == 0

    checks: dict[str, tuple[str, str, str]] = {}

    checks["stock_reorder"] = (
        _needs(
            WAITING,
            "nothing is being tracked in stock",
            "Count what is in the store and the walk-in, then import it. Until then Rain "
            "has nothing to reorder against.",
        )
        if f["stock_items"] == 0
        else _needs(
            PARTIAL,
            f"{f['stock_items']} item(s) tracked, but no supplier to order from",
            "Add who you buy from — a purchase order needs a supplier, a minimum order and "
            "a delivery day.",
        )
        if f["suppliers"] == 0
        else _needs(
            PARTIAL,
            f"{f['stock_items']} item(s) tracked, no reorder points set",
            "Rain sets these himself from usage — but he needs a few weeks of real sales "
            "first. Until then he is guessing.",
        )
        if f["reorder_policies"] == 0 or not sales
        else _needs(READY, f"{f['stock_items']} item(s), {f['suppliers']} supplier(s)")
    )

    checks["supplier_invoice"] = (
        _needs(
            WAITING,
            "no suppliers on file",
            "Suri matches invoices against orders and deliveries. With no supplier there "
            "is nothing to match.",
        )
        if f["suppliers"] == 0
        else _needs(READY, f"{f['suppliers']} supplier(s) on file")
    )

    checks["prep_forecaster"] = (
        no_sales
        if not sales
        else _needs(
            PARTIAL,
            "sales exist, but dishes have no recipes",
            f"{f['uncosted_dishes']} dish(es) have no recipe, so Betrisha cannot turn a "
            "forecast into ingredient quantities. She can predict covers, not a prep list.",
        )
        if not costs
        else _needs(READY, "sales history and recipes both present")
    )

    checks["order_pacing"] = _needs(
        WAITING,
        "no live order feed",
        "Ciknor sequences tickets as they arrive, which needs the POS posting orders to "
        "POST /webhooks/pos. Hand-recorded sales arrive after service, too late to pace.",
    )

    checks["menu_pricing"] = (
        _needs(
            WAITING,
            "no menu imported",
            "`restaurant-ai import-menu <file.xlsx>` — the real menu is in the repo under menu/.",
        )
        if f["dishes"] == 0
        else _needs(
            PARTIAL,
            f"{f['uncosted_dishes']} of {f['dishes']} dishes have no recipe",
            "A dish with no recipe costs nothing, so it shows 100% margin and drags the "
            "average up with it. Irma will call your worst dishes stars.",
        )
        if f["uncosted_dishes"]
        else no_sales
        if not sales
        else _needs(READY, f"{f['dishes']} dishes, all costed, with sales to rank them by")
    )

    checks["bookkeeping"] = (
        no_sales if not sales else _needs(READY, f"{f['real_orders']} real order(s) to reconcile")
    )

    checks["daily_performance"] = (
        no_sales
        if not sales
        else _needs(
            PARTIAL,
            "revenue is real, prime cost is not",
            "Uncosted dishes make food cost look better than it is. The revenue line is "
            "trustworthy; the margin line is not.",
        )
        if not costs
        else _needs(READY, "revenue and cost both real")
    )

    checks["shift_scheduling"] = (
        _needs(
            WAITING,
            "no staff on file",
            "Henry needs who works here, their roles, and when they can work. Without that "
            "there is no roster to build.",
        )
        if f["staff"] == 0
        else _needs(
            PARTIAL,
            f"{f['staff']} staff, but nobody's availability is recorded",
            "A roster built without availability rosters people who cannot come.",
        )
        if f["availability"] == 0
        else _needs(READY, f"{f['staff']} staff with availability")
    )

    checks["social_content"] = (
        _needs(WAITING, "no menu to write about", "`restaurant-ai import-menu <file.xlsx>`.")
        if f["dishes"] == 0
        else _needs(
            PARTIAL,
            "Franky can post, but not choose what to push",
            "He features what needs moving, which is a margin question — and margins are "
            "fiction until the dishes are costed.",
        )
        if not costs
        else _needs(READY, f"{f['dishes']} dishes, costed")
    )

    checks["staff_assistant"] = (
        _needs(
            WAITING,
            "no SOPs to answer from",
            "Kaksu answers from written procedure, never from memory. With none loaded he "
            "can only say he does not know.",
        )
        if f["sops"] == 0
        else _needs(READY, f"{f['sops']} procedure(s) on file")
    )

    # Aziera's diary is the one thing here that needs nothing but the owner.
    checks["reputation"] = _needs(
        READY,
        "the diary works from the day you use it"
        + (f"; {f['reviews']} review(s) stored" if f["reviews"] else "; no reviews yet"),
    )

    for name, spec in sorted(all_agents().items()):
        state, detail, fix = checks.get(name, _needs(READY, "no data of its own required"))
        picture.agents.append(
            Readiness(agent=name, person=spec.person or name, state=state, detail=detail, fix=fix)
        )
    return picture


def render(picture: Picture) -> str:
    f = picture.facts
    marks = {READY: "real ", PARTIAL: "part ", WAITING: "WAIT "}
    lines = ["What is real, and what is still a demonstration", ""]

    if f["demo_orders"] and not f["real_orders"]:
        lines.append(
            f"  Every number here comes from {f['demo_orders']} invented orders. Nothing "
            "below is about your restaurant yet."
        )
    elif f["demo_orders"]:
        lines.append(
            f"  {f['real_orders']} real order(s) mixed with {f['demo_orders']} invented "
            "ones. Totals are the two added together."
        )
    else:
        lines.append(f"  {f['real_orders']} real order(s), no demo data.")
    lines.append("")

    width = max(len(a.person) for a in picture.agents)
    for a in picture.agents:
        lines.append(f"  {marks[a.state]} {a.person.ljust(width)}  {a.detail}")

    blocked = [a for a in picture.agents if a.fix]
    if blocked:
        lines.append("")
        for a in blocked:
            lines.append(f"  {a.person}: {a.fix}")

    lines.append("")
    lines.append(
        f"  {len(picture.ready)} of {len(picture.agents)} agents can tell you the truth today."
    )
    return "\n".join(lines)
