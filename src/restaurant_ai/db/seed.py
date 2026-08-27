"""Load the demo restaurant.

Idempotent: re-running updates reference data in place rather than duplicating
it, so `make seed` is safe to repeat.

``seed_history`` additionally synthesises past trading days. Without history the
prep forecaster, reorder-point calculator and menu-engineering classifier have
nothing to learn from, so a freshly seeded database would produce empty output
from half the platform.
"""

from __future__ import annotations

import random
from datetime import datetime, time, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from restaurant_ai import clock
from restaurant_ai.db.base import money, qty, session_scope
from restaurant_ai.db.models import (
    Allergen,
    Availability,
    Guest,
    Ingredient,
    LedgerAccount,
    MenuItem,
    MenuSection,
    MovementReason,
    OrderChannel,
    OrderHeader,
    OrderLine,
    OrderStatus,
    Payment,
    PaymentMethod,
    Recipe,
    RecipeComponent,
    ReorderPolicy,
    SopDocument,
    Staff,
    StockItem,
    StockMovement,
    Supplier,
    TableDef,
)
from restaurant_ai.db.seed_data import (
    ALLERGENS,
    INGREDIENTS,
    LEDGER_ACCOUNTS,
    MENU_ITEMS,
    MENU_SECTIONS,
    SOPS,
    STAFF,
    STOCK_ITEMS,
    SUB_RECIPES,
    SUPPLIERS,
    TABLES,
)
from restaurant_ai.logging_setup import get_logger

log = get_logger(__name__)

# Relative demand by weekday (0=Mon). Friday and Saturday carry the week.
WEEKDAY_FACTOR = [0.72, 0.75, 0.85, 0.98, 1.35, 1.48, 1.10]
BASE_COVERS = 96

# Share of covers by channel.
CHANNEL_MIX: list[tuple[OrderChannel, float]] = [
    (OrderChannel.DINE_IN, 0.55),
    (OrderChannel.TAKEAWAY, 0.15),
    (OrderChannel.DELIVERY, 0.20),
    (OrderChannel.DRIVE_THRU, 0.06),
    (OrderChannel.KIOSK, 0.04),
]

DELIVERY_PLATFORMS = ["GrabFood", "foodpanda"]


def _upsert(session: Session, model, match: dict, values: dict):
    """Fetch-or-create, then update. Keeps seeding idempotent."""
    stmt = select(model).filter_by(**match)
    obj = session.execute(stmt).scalar_one_or_none()
    if obj is None:
        obj = model(**match, **values)
        session.add(obj)
    else:
        for k, v in values.items():
            setattr(obj, k, v)
    return obj


def seed_reference(session: Session) -> dict[str, int]:
    """Load menu, recipes, suppliers, staff, tables, accounts and SOPs."""
    counts: dict[str, int] = {}

    for code, label in ALLERGENS:
        _upsert(session, Allergen, {"code": code}, {"label": label})
    counts["allergens"] = len(ALLERGENS)

    ingredients: dict[str, Ingredient] = {}
    for code, name, uom, cost, yield_pct, shelf, allergens in INGREDIENTS:
        ingredients[code] = _upsert(
            session,
            Ingredient,
            {"code": code},
            {
                "name": name,
                "base_uom": uom,
                "cost_per_base_unit": qty(cost),
                "yield_pct": qty(yield_pct),
                "shelf_life_days": shelf,
                "allergen_codes": allergens,
            },
        )
    session.flush()
    counts["ingredients"] = len(ingredients)

    sections: dict[str, MenuSection] = {}
    for name, order in MENU_SECTIONS:
        sections[name] = _upsert(session, MenuSection, {"name": name}, {"display_order": order})
    session.flush()

    # Sub-recipes first: plated recipes reference them.
    recipes: dict[str, Recipe] = {}
    for code, name, y_qty, y_uom, station, prep_s, components in SUB_RECIPES:
        recipe = _upsert(
            session,
            Recipe,
            {"code": code},
            {
                "name": name,
                "yield_qty": qty(y_qty),
                "yield_uom": y_uom,
                "station": station,
                "prep_seconds": prep_s,
                "menu_item_id": None,
            },
        )
        session.flush()
        recipe.components.clear()
        session.flush()
        for ing_code, c_qty, c_uom in components:
            recipe.components.append(
                RecipeComponent(
                    ingredient_id=ingredients[ing_code].id, quantity=qty(c_qty), uom=c_uom
                )
            )
        recipes[code] = recipe
    session.flush()
    counts["sub_recipes"] = len(recipes)

    menu_items: dict[str, MenuItem] = {}
    for sku, name, section, price, station, prep_s, course, desc, components in MENU_ITEMS:
        item = _upsert(
            session,
            MenuItem,
            {"sku": sku},
            {
                "name": name,
                "description": desc,
                "section_id": sections[section].id,
                "price": money(price),
                "station": station,
                "prep_seconds": prep_s,
                "course": course,
                "is_active": True,
            },
        )
        session.flush()
        recipe = _upsert(
            session,
            Recipe,
            {"code": f"REC-{sku.split('-', 1)[1]}"},
            {
                "name": name,
                "menu_item_id": item.id,
                "yield_qty": qty("1"),
                "yield_uom": "ea",
                "station": station,
                "prep_seconds": prep_s,
            },
        )
        session.flush()
        recipe.components.clear()
        session.flush()
        for comp_code, c_qty, c_uom in components:
            if comp_code in recipes:  # a sub-recipe
                recipe.components.append(
                    RecipeComponent(
                        sub_recipe_id=recipes[comp_code].id, quantity=qty(c_qty), uom=c_uom
                    )
                )
            else:
                recipe.components.append(
                    RecipeComponent(
                        ingredient_id=ingredients[comp_code].id, quantity=qty(c_qty), uom=c_uom
                    )
                )
        menu_items[sku] = item
    session.flush()
    counts["menu_items"] = len(menu_items)

    suppliers: dict[str, Supplier] = {}
    for code, name, email, lead, min_val, days in SUPPLIERS:
        suppliers[code] = _upsert(
            session,
            Supplier,
            {"code": code},
            {
                "name": name,
                "email": email,
                "lead_time_days": lead,
                "min_order_value": money(min_val),
                "delivery_days": days,
                "is_active": True,
            },
        )
    session.flush()
    counts["suppliers"] = len(suppliers)

    for ing_code, sup_code, sku, pack, pack_uom, price, moq in STOCK_ITEMS:
        _upsert(
            session,
            StockItem,
            {"supplier_id": suppliers[sup_code].id, "supplier_sku": sku},
            {
                "ingredient_id": ingredients[ing_code].id,
                "pack_size": qty(pack),
                "pack_uom": pack_uom,
                "contract_price": money(price),
                "min_order_qty": qty(moq),
                "is_preferred": True,
            },
        )
    counts["stock_items"] = len(STOCK_ITEMS)

    for label, seats, section, combinable in TABLES:
        _upsert(
            session,
            TableDef,
            {"label": label},
            {
                "seats": seats,
                "section": section,
                "is_combinable": combinable,
                "min_party": 1 if seats <= 2 else max(1, seats - 3),
                "is_active": True,
            },
        )
    counts["tables"] = len(TABLES)

    for code, name, role, rate, max_hours in STAFF:
        staff = _upsert(
            session,
            Staff,
            {"employee_code": code},
            {
                "name": name,
                "role": role,
                "hourly_rate": money(str(rate)),
                "max_weekly_hours": max_hours,
                "is_active": True,
            },
        )
        session.flush()
        # Everyone is available Tue-Sun; the restaurant is closed Monday.
        if not staff.availability:
            for weekday in range(1, 7):
                staff.availability.append(
                    Availability(weekday=weekday, start_time=time(10, 0), end_time=time(23, 0))
                )
    counts["staff"] = len(STAFF)

    for code, name, acc_type in LEDGER_ACCOUNTS:
        _upsert(session, LedgerAccount, {"code": code}, {"name": name, "type": acc_type})
    counts["ledger_accounts"] = len(LEDGER_ACCOUNTS)

    for slug, title, category, role, body in SOPS:
        _upsert(
            session,
            SopDocument,
            {"slug": slug},
            {"title": title, "category": category, "applies_to_role": role, "body": body},
        )
    counts["sops"] = len(SOPS)

    return counts


def seed_opening_stock(session: Session, days_cover: int = 6) -> int:
    """Give every ingredient an opening balance, sized from menu demand.

    Without this the first reorder sweep would flag the entire catalogue as
    out of stock, which is technically correct but useless as a demo.
    """
    from restaurant_ai.domain.costing import explode_menu_item

    ingredients = {i.id: i for i in session.execute(select(Ingredient)).scalars()}
    items = list(session.execute(select(MenuItem)).scalars())

    daily_usage: dict[str, Decimal] = dict.fromkeys(ingredients, Decimal("0"))
    for item in items:
        expected_daily = Decimal(str(BASE_COVERS)) / Decimal(len(items))
        for ing_id, amount in explode_menu_item(session, item.id).items():
            daily_usage[ing_id] = daily_usage.get(ing_id, Decimal("0")) + amount * expected_daily

    now = clock.now()
    created = 0
    for ing_id, per_day in daily_usage.items():
        if per_day <= 0:
            continue
        opening = (per_day * Decimal(days_cover)).quantize(Decimal("0.0001"))
        existing = session.execute(
            select(StockMovement).where(
                StockMovement.ingredient_id == ing_id,
                StockMovement.source_type == "seed_opening",
            )
        ).scalar_one_or_none()
        if existing is not None:
            existing.quantity = opening
            continue
        session.add(
            StockMovement(
                ingredient_id=ing_id,
                quantity=opening,
                reason=MovementReason.COUNT_ADJUSTMENT,
                unit_cost=ingredients[ing_id].cost_per_base_unit,
                occurred_at=now,
                source_type="seed_opening",
                source_id="seed",
                note="Opening balance from seed",
            )
        )
        _upsert(
            session,
            ReorderPolicy,
            {"ingredient_id": ing_id},
            {
                "avg_daily_usage": per_day.quantize(Decimal("0.0001")),
                "target_days_cover": 7,
            },
        )
        created += 1
    return created


def seed_history(session: Session, days: int = 56, seed: int = 20260101) -> int:
    """Synthesise past trading days so the analytics agents have a baseline.

    Demand is a weekday factor times a slow upward trend times noise, with a
    per-item popularity weight, which is exactly the structure the forecaster is
    designed to recover.
    """
    rng = random.Random(seed)
    items = list(session.execute(select(MenuItem).where(MenuItem.is_active)).scalars())
    if not items:
        return 0

    # Stable per-item popularity: drinks and headline mains sell hardest.
    popularity: dict[str, float] = {}
    for item in items:
        base = 1.0
        if item.course == 4:
            base = 1.9
        elif item.sku in ("MNU-NASILEMK", "MNU-CHARKWAY"):
            base = 2.2
        elif item.course == 1:
            base = 0.75
        elif item.course == 3:
            base = 0.6
        popularity[item.id] = base * rng.uniform(0.85, 1.15)
    weight_total = sum(popularity.values())

    guests = _ensure_guests(session, rng, count=40)
    today = clock.today()
    orders_created = 0
    counter = 0

    for offset in range(days, 0, -1):
        day = today - timedelta(days=offset)
        if day.weekday() == 0:  # closed Mondays
            continue

        trend = 1.0 + (days - offset) * 0.0012
        covers = int(BASE_COVERS * WEEKDAY_FACTOR[day.weekday()] * trend * rng.uniform(0.9, 1.1))

        existing = session.execute(
            select(OrderHeader.id).where(OrderHeader.business_date == day).limit(1)
        ).scalar_one_or_none()
        if existing is not None:
            continue

        n_orders = max(1, covers // 2)
        for _ in range(n_orders):
            counter += 1
            channel = _pick_channel(rng)
            hour = _pick_hour(rng)
            placed = datetime.combine(
                day, time(hour, rng.randrange(0, 60)), tzinfo=clock.local_tz()
            )
            party = rng.choices([1, 2, 3, 4, 5, 6], weights=[12, 38, 16, 22, 7, 5])[0]

            order = OrderHeader(
                order_number=f"H{day.strftime('%y%m%d')}-{counter:05d}",
                channel=channel,
                status=OrderStatus.CLOSED,
                party_size=party,
                placed_at=placed,
                closed_at=placed + timedelta(minutes=rng.randrange(35, 95)),
                business_date=day,
                delivery_platform=(
                    rng.choice(DELIVERY_PLATFORMS) if channel == OrderChannel.DELIVERY else None
                ),
                external_ref=f"POS-{day.strftime('%y%m%d')}-{counter:05d}",
            )
            if rng.random() < 0.35 and guests:
                guest = rng.choice(guests)
                order.guest_id = guest.id
                guest.visit_count += 1
                guest.last_visit_on = day
                if guest.first_visit_on is None or day < guest.first_visit_on:
                    guest.first_visit_on = day

            subtotal = Decimal("0")
            n_lines = min(party + rng.randrange(0, 3), 7)
            chosen = _weighted_sample(rng, items, popularity, weight_total, n_lines)
            for item in chosen:
                line_qty = 1 if item.course != 4 else rng.choices([1, 2], weights=[7, 3])[0]
                line_total = money(item.price * line_qty)
                order.lines.append(
                    OrderLine(
                        menu_item_id=item.id,
                        quantity=qty(str(line_qty)),
                        unit_price=item.price,
                        line_total=line_total,
                        course=item.course,
                        stock_deducted_at=placed,
                    )
                )
                subtotal += line_total

            tax = money(subtotal * Decimal("0.06"))
            order.subtotal = money(subtotal)
            order.tax = tax
            order.total = money(subtotal + tax)

            method = _payment_method(rng, channel)
            order.payments.append(
                Payment(
                    method=method,
                    amount=order.total,
                    tip=(
                        money(order.total * Decimal(str(rng.uniform(0, 0.05))))
                        if channel == OrderChannel.DINE_IN
                        else Decimal("0")
                    ),
                    paid_at=order.closed_at or placed,
                    business_date=day,
                    processor_ref=(
                        f"TXN-{day.strftime('%y%m%d')}-{counter:05d}"
                        if method != PaymentMethod.CASH
                        else None
                    ),
                    is_reconciled=True,
                )
            )
            session.add(order)
            orders_created += 1

        session.flush()

    return orders_created


def _ensure_guests(session: Session, rng: random.Random, count: int) -> list[Guest]:
    existing = list(session.execute(select(Guest)).scalars())
    if len(existing) >= count:
        return existing
    first = [
        "Amir",
        "Siti",
        "Wei",
        "Priya",
        "Hafiz",
        "Mei",
        "Ravi",
        "Nurul",
        "Jia",
        "Farah",
        "Kumar",
        "Ling",
        "Adam",
        "Zara",
        "Chong",
        "Aina",
        "Devan",
        "Yee",
        "Rizal",
        "Anita",
    ]
    last = ["Rahman", "Tan", "Nair", "Wong", "Ismail", "Lim", "Kaur", "Abdullah", "Chen", "Menon"]
    created = list(existing)
    for _ in range(len(existing), count):
        created.append(
            Guest(
                name=f"{rng.choice(first)} {rng.choice(last)}",
                phone=f"+60{rng.randrange(10, 20)}{rng.randrange(1000000, 9999999)}",
                visit_count=0,
                marketing_opt_in=rng.random() < 0.8,
            )
        )
        session.add(created[-1])
    session.flush()
    return created


def _pick_channel(rng: random.Random) -> OrderChannel:
    r = rng.random()
    cumulative = 0.0
    for channel, share in CHANNEL_MIX:
        cumulative += share
        if r <= cumulative:
            return channel
    return OrderChannel.DINE_IN


def _pick_hour(rng: random.Random) -> int:
    """Two peaks: lunch around 12-13, dinner around 19-20."""
    if rng.random() < 0.42:
        return rng.choices([11, 12, 13, 14], weights=[2, 5, 4, 2])[0]
    return rng.choices([17, 18, 19, 20, 21, 22], weights=[2, 4, 6, 6, 4, 2])[0]


def _payment_method(rng: random.Random, channel: OrderChannel) -> PaymentMethod:
    if channel == OrderChannel.DELIVERY:
        return PaymentMethod.DELIVERY_PLATFORM
    return rng.choices(
        [PaymentMethod.CARD, PaymentMethod.EWALLET, PaymentMethod.CASH], weights=[55, 30, 15]
    )[0]


def _weighted_sample(
    rng: random.Random,
    items: list[MenuItem],
    popularity: dict[str, float],
    total: float,
    k: int,
) -> list[MenuItem]:
    """Sample k distinct items weighted by popularity."""
    pool = list(items)
    weights = [popularity[i.id] for i in pool]
    chosen: list[MenuItem] = []
    for _ in range(min(k, len(pool))):
        pick = rng.choices(pool, weights=weights)[0]
        idx = pool.index(pick)
        pool.pop(idx)
        weights.pop(idx)
        chosen.append(pick)
    return chosen


def seed_all(
    session: Session | None = None, history_days: int = 56, with_stock: bool = True
) -> dict[str, int]:
    with session_scope(session) as db:
        counts = seed_reference(db)
        db.flush()
        if with_stock:
            counts["opening_stock"] = seed_opening_stock(db)
        counts["historical_orders"] = seed_history(db, days=history_days)
        log.info("seed complete", **counts)
        return counts


__all__ = ["seed_all", "seed_reference", "seed_history", "seed_opening_stock"]
