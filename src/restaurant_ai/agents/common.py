"""Queries shared across agents.

On-hand stock is derived by summing the append-only movement ledger rather than
read from a mutable column, so every figure an agent acts on traces back to the
sale or delivery that caused it.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from restaurant_ai.db.models import (
    Ingredient,
    MenuItem,
    OrderHeader,
    OrderLine,
    OrderStatus,
    PurchaseOrder,
    PurchaseOrderLine,
    PurchaseOrderStatus,
    StockItem,
    StockMovement,
    Supplier,
)
from restaurant_ai.domain.costing import explode_menu_item
from restaurant_ai.domain.forecasting import DailySales

ZERO = Decimal("0")


def on_hand(session: Session, ingredient_id: str) -> Decimal:
    total = session.execute(
        select(func.coalesce(func.sum(StockMovement.quantity), 0)).where(
            StockMovement.ingredient_id == ingredient_id
        )
    ).scalar_one()
    return Decimal(str(total))


def on_hand_all(session: Session) -> dict[str, Decimal]:
    """Current balance for every ingredient, in one query."""
    rows = session.execute(
        select(StockMovement.ingredient_id, func.sum(StockMovement.quantity)).group_by(
            StockMovement.ingredient_id
        )
    ).all()
    balances = {ingredient_id: Decimal(str(total)) for ingredient_id, total in rows}
    for ingredient in session.execute(select(Ingredient.id)).scalars():
        balances.setdefault(ingredient, ZERO)
    return balances


def on_order_all(session: Session) -> dict[str, Decimal]:
    """Quantities already ordered but not yet received, in ingredient base units.

    Counted so the reorder sweep does not order the same shortfall twice while
    the first delivery is still in transit.
    """
    rows = session.execute(
        select(
            StockItem.ingredient_id,
            func.sum(
                (PurchaseOrderLine.quantity_packs - PurchaseOrderLine.received_packs)
                * StockItem.pack_size
            ),
        )
        .join(PurchaseOrderLine, PurchaseOrderLine.stock_item_id == StockItem.id)
        .join(PurchaseOrder, PurchaseOrderLine.purchase_order_id == PurchaseOrder.id)
        .where(
            PurchaseOrder.status.in_(
                [
                    PurchaseOrderStatus.APPROVED,
                    PurchaseOrderStatus.SENT,
                    PurchaseOrderStatus.PARTIALLY_RECEIVED,
                ]
            )
        )
        .group_by(StockItem.ingredient_id)
    ).all()
    return {ingredient_id: Decimal(str(total or 0)) for ingredient_id, total in rows}


def sales_history(session: Session, days: int, until: date | None = None) -> list[DailySales]:
    """Per-item daily unit sales, the input to every forecast."""
    until = until or date.today()
    since = until - timedelta(days=days)
    rows = session.execute(
        select(
            OrderHeader.business_date,
            OrderLine.menu_item_id,
            func.sum(OrderLine.quantity),
        )
        .join(OrderLine, OrderLine.order_id == OrderHeader.id)
        .where(
            OrderHeader.business_date >= since,
            OrderHeader.business_date < until,
            OrderHeader.status != OrderStatus.VOID,
            OrderLine.is_voided.is_(False),
        )
        .group_by(OrderHeader.business_date, OrderLine.menu_item_id)
    ).all()
    return [
        DailySales(business_date=day, menu_item_id=item_id, quantity=Decimal(str(qty)))
        for day, item_id, qty in rows
    ]


def units_sold_on(session: Session, business_date: date) -> dict[str, Decimal]:
    rows = session.execute(
        select(OrderLine.menu_item_id, func.sum(OrderLine.quantity))
        .join(OrderHeader, OrderLine.order_id == OrderHeader.id)
        .where(
            OrderHeader.business_date == business_date,
            OrderHeader.status != OrderStatus.VOID,
            OrderLine.is_voided.is_(False),
        )
        .group_by(OrderLine.menu_item_id)
    ).all()
    return {item_id: Decimal(str(qty)) for item_id, qty in rows}


def units_sold_between(session: Session, start: date, end: date) -> dict[str, Decimal]:
    rows = session.execute(
        select(OrderLine.menu_item_id, func.sum(OrderLine.quantity))
        .join(OrderHeader, OrderLine.order_id == OrderHeader.id)
        .where(
            OrderHeader.business_date >= start,
            OrderHeader.business_date <= end,
            OrderHeader.status != OrderStatus.VOID,
            OrderLine.is_voided.is_(False),
        )
        .group_by(OrderLine.menu_item_id)
    ).all()
    return {item_id: Decimal(str(qty)) for item_id, qty in rows}


def daily_ingredient_usage(
    session: Session, days: int, until: date | None = None
) -> dict[str, list[Decimal]]:
    """Ingredient usage per trading day, derived by exploding each day's sales.

    Used to refresh reorder policies: the mean gives average daily usage and the
    spread gives the sigma that sizes safety stock.
    """
    until = until or date.today()
    history = sales_history(session, days, until)

    by_day: dict[date, dict[str, Decimal]] = defaultdict(dict)
    for row in history:
        by_day[row.business_date][row.menu_item_id] = row.quantity

    # Explode each menu item once, then scale, rather than re-walking the BOM
    # for every day: the recipe tree does not change between days.
    per_unit: dict[str, dict[str, Decimal]] = {}
    for day_items in by_day.values():
        for menu_item_id in day_items:
            if menu_item_id not in per_unit:
                per_unit[menu_item_id] = explode_menu_item(session, menu_item_id, 1)

    usage: dict[str, list[Decimal]] = defaultdict(list)
    for _day, items in sorted(by_day.items()):
        day_totals: dict[str, Decimal] = defaultdict(Decimal)
        for menu_item_id, quantity in items.items():
            for ingredient_id, amount in per_unit.get(menu_item_id, {}).items():
                day_totals[ingredient_id] += amount * quantity
        for ingredient_id, total in day_totals.items():
            usage[ingredient_id].append(total)
    return dict(usage)


def active_menu_items(session: Session) -> list[MenuItem]:
    return list(session.execute(select(MenuItem).where(MenuItem.is_active)).scalars())


def preferred_stock_items(session: Session) -> dict[str, tuple[StockItem, Supplier]]:
    """The pack each ingredient is normally bought as, with its supplier."""
    rows = session.execute(
        select(StockItem, Supplier)
        .join(Supplier, StockItem.supplier_id == Supplier.id)
        .where(Supplier.is_active)
        .order_by(StockItem.is_preferred.desc(), StockItem.contract_price)
    ).all()
    chosen: dict[str, tuple[StockItem, Supplier]] = {}
    for stock_item, supplier in rows:
        chosen.setdefault(stock_item.ingredient_id, (stock_item, supplier))
    return chosen


def next_po_number(session: Session, business_date: date) -> str:
    prefix = f"PO-{business_date.strftime('%y%m%d')}"
    count = session.execute(
        select(func.count(PurchaseOrder.id)).where(PurchaseOrder.po_number.like(f"{prefix}%"))
    ).scalar_one()
    return f"{prefix}-{count + 1:03d}"


def revenue_on(session: Session, business_date: date) -> Decimal:
    total = session.execute(
        select(func.coalesce(func.sum(OrderHeader.subtotal), 0)).where(
            OrderHeader.business_date == business_date,
            OrderHeader.status != OrderStatus.VOID,
        )
    ).scalar_one()
    return Decimal(str(total))


def covers_on(session: Session, business_date: date) -> int:
    total = session.execute(
        select(func.coalesce(func.sum(OrderHeader.party_size), 0)).where(
            OrderHeader.business_date == business_date,
            OrderHeader.status != OrderStatus.VOID,
        )
    ).scalar_one()
    return int(total or 0)
