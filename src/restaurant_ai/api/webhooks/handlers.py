"""What actually happens when a webhook lands.

The POS handler is the important one: a sale writes the order, explodes it
through the recipe BOM, and deducts every ingredient from stock. That is the
mechanism by which selling a nasi lemak moves 180 g of chicken thigh and 2 g of
belacan, and by which the reorder agent finds out it needs to order more.

Deductions are guarded by ``stock_deducted_at`` on the order line, so a
reprocessed event cannot deduct the same sale twice.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from restaurant_ai import clock
from restaurant_ai.db.models import (
    Ingredient,
    MenuItem,
    MovementReason,
    OrderChannel,
    OrderHeader,
    OrderLine,
    OrderStatus,
    Payment,
    PaymentMethod,
    ReorderPolicy,
    StockMovement,
)
from restaurant_ai.domain.costing import explode_menu_item
from restaurant_ai.events import Event, Topic, publish
from restaurant_ai.logging_setup import get_logger

log = get_logger(__name__)
ZERO = Decimal("0")


def handle_pos_order(
    payload: dict[str, Any], session: Session, run_id: str | None = None
) -> dict[str, Any]:
    """Record a sale and deduct its ingredients from stock."""
    external_id = payload.get("external_id") or payload.get("order_id")
    if not external_id:
        raise ValueError("POS payload has no external_id.")

    existing = session.execute(
        select(OrderHeader).where(OrderHeader.external_ref == external_id)
    ).scalar_one_or_none()
    if existing is not None:
        return {"order_id": existing.id, "duplicate": True}

    placed_at = _parse_dt(payload.get("placed_at")) or clock.now()
    business_date = placed_at.date()

    order = OrderHeader(
        order_number=payload.get("order_number") or f"W{external_id}",
        channel=OrderChannel(payload.get("channel", "dine_in")),
        status=OrderStatus.OPEN,
        party_size=int(payload.get("party_size", 1)),
        placed_at=placed_at,
        business_date=business_date,
        delivery_platform=payload.get("delivery_platform"),
        external_ref=external_id,
        notes=payload.get("notes"),
    )
    session.add(order)
    session.flush()

    skus = [line["sku"] for line in payload.get("lines", [])]
    menu = {
        item.sku: item
        for item in session.execute(
            select(MenuItem).where(MenuItem.sku.in_(skus or [""]))
        ).scalars()
    }

    subtotal = ZERO
    for raw in payload.get("lines", []):
        item = menu.get(raw["sku"])
        if item is None:
            log.warning("unknown sku on POS order", sku=raw["sku"], order=external_id)
            continue
        quantity = Decimal(str(raw.get("quantity", 1)))
        unit_price = Decimal(str(raw.get("unit_price", item.price)))
        line_total = (unit_price * quantity).quantize(Decimal("0.01"))
        session.add(
            OrderLine(
                order_id=order.id,
                menu_item_id=item.id,
                quantity=quantity,
                unit_price=unit_price,
                line_total=line_total,
                course=raw.get("course", item.course),
                modifiers=raw.get("modifiers"),
            )
        )
        subtotal += line_total

    tax = (subtotal * Decimal("0.06")).quantize(Decimal("0.01"))
    order.subtotal = subtotal
    order.tax = tax
    order.total = subtotal + tax
    session.flush()

    method = payload.get("payment_method")
    if method:
        session.add(
            Payment(
                order_id=order.id,
                method=PaymentMethod(method),
                amount=order.total,
                tip=Decimal(str(payload.get("tip", "0"))),
                paid_at=placed_at,
                business_date=business_date,
                processor_ref=payload.get("processor_ref"),
            )
        )
        order.status = OrderStatus.CLOSED
        order.closed_at = placed_at

    deducted = deduct_stock_for_order(order.id, session)
    session.flush()

    publish(
        Event(
            Topic.ORDER_PLACED,
            {
                "order_number": order.order_number,
                "channel": order.channel.value,
                "total": str(order.total),
            },
            source_run_id=run_id,
        ),
        session=session,
    )

    return {
        "order_id": order.id,
        "order_number": order.order_number,
        "total": str(order.total),
        "lines": len(payload.get("lines", [])),
        "ingredients_deducted": deducted,
        "duplicate": False,
    }


def deduct_stock_for_order(order_id: str, session: Session) -> int:
    """Explode each line through its recipe and write the stock movements.

    ``stock_deducted_at`` on the line is the guard: a line already deducted is
    skipped, so reprocessing an event cannot double-deduct a sale.
    """
    lines = list(
        session.execute(
            select(OrderLine).where(
                OrderLine.order_id == order_id,
                OrderLine.stock_deducted_at.is_(None),
                OrderLine.is_voided.is_(False),
            )
        ).scalars()
    )
    if not lines:
        return 0

    order = session.get(OrderHeader, order_id)
    occurred_at = order.placed_at if order else clock.now()

    totals: dict[str, Decimal] = {}
    for line in lines:
        for ingredient_id, amount in explode_menu_item(
            session, line.menu_item_id, line.quantity
        ).items():
            totals[ingredient_id] = totals.get(ingredient_id, ZERO) + amount
        line.stock_deducted_at = clock.utcnow()

    if not totals:
        return 0

    costs = {
        i.id: i.cost_per_base_unit
        for i in session.execute(
            select(Ingredient).where(Ingredient.id.in_(list(totals)))
        ).scalars()
    }

    for ingredient_id, amount in totals.items():
        if amount <= 0:
            continue
        session.add(
            StockMovement(
                ingredient_id=ingredient_id,
                quantity=-amount,  # negative: stock leaving
                reason=MovementReason.SALE,
                unit_cost=costs.get(ingredient_id, ZERO),
                occurred_at=occurred_at,
                source_type="order",
                source_id=order_id,
            )
        )

    session.flush()
    _flag_low_stock(session, list(totals))
    return len(totals)


def _flag_low_stock(session: Session, ingredient_ids: list[str]) -> None:
    """Emit stock.low for anything this sale pushed under its reorder point.

    This is the event-driven half of reordering: the scheduled sweep catches
    everything twice a day, but a rush on one dish can empty an ingredient
    between sweeps.
    """
    from restaurant_ai.agents.common import on_hand

    policies = {
        p.ingredient_id: p
        for p in session.execute(
            select(ReorderPolicy).where(ReorderPolicy.ingredient_id.in_(ingredient_ids))
        ).scalars()
    }
    for ingredient_id, policy in policies.items():
        if policy.reorder_point <= 0:
            continue
        if on_hand(session, ingredient_id) <= policy.reorder_point:
            publish(
                Event(Topic.STOCK_LOW, {"ingredient_id": ingredient_id}),
                session=session,
            )


def handle_payment(
    payload: dict[str, Any], session: Session, run_id: str | None = None
) -> dict[str, Any]:
    """Attach a settlement reference to an existing order's payment."""
    external_ref = payload.get("order_external_id")
    order = session.execute(
        select(OrderHeader).where(OrderHeader.external_ref == external_ref)
    ).scalar_one_or_none()
    if order is None:
        return {"matched": False, "reason": f"No order with external ref {external_ref!r}."}

    payment = session.execute(
        select(Payment).where(Payment.order_id == order.id)
    ).scalar_one_or_none()
    if payment is None:
        payment = Payment(
            order_id=order.id,
            method=PaymentMethod(payload.get("method", "card")),
            amount=Decimal(str(payload.get("amount", order.total))),
            paid_at=_parse_dt(payload.get("paid_at")) or clock.now(),
            business_date=order.business_date,
        )
        session.add(payment)

    payment.processor_ref = payload.get("processor_ref")
    session.flush()
    return {"matched": True, "order_number": order.order_number}


def handle_review(
    payload: dict[str, Any], session: Session, run_id: str | None = None
) -> dict[str, Any]:
    """Store an inbound review; the reputation agent classifies it on its sweep."""
    from restaurant_ai.db.models import Review

    existing = session.execute(
        select(Review).where(
            Review.platform == payload["platform"],
            Review.external_id == payload["external_id"],
        )
    ).scalar_one_or_none()
    if existing is not None:
        return {"stored": False, "duplicate": True}

    posted_at = _parse_dt(payload.get("posted_at")) or clock.now()
    session.add(
        Review(
            platform=payload["platform"],
            external_id=payload["external_id"],
            author=payload.get("author", "Anonymous"),
            rating=int(payload.get("rating", 3)),
            body=payload.get("body", ""),
            posted_at=posted_at,
            business_date=posted_at.date(),
        )
    )
    session.flush()
    publish(
        Event(Topic.REVIEW_RECEIVED, {"platform": payload["platform"]}, source_run_id=run_id),
        session=session,
    )
    return {"stored": True, "duplicate": False}


def handle_delivery_payout(
    payload: dict[str, Any], session: Session, run_id: str | None = None
) -> dict[str, Any]:
    """Record a platform settlement, net of commission."""
    from datetime import date

    from restaurant_ai.db.models import DeliveryPayout

    existing = session.execute(
        select(DeliveryPayout).where(DeliveryPayout.payout_ref == payload["payout_ref"])
    ).scalar_one_or_none()
    if existing is not None:
        return {"stored": False, "duplicate": True}

    gross = Decimal(str(payload["gross_sales"]))
    commission = Decimal(str(payload["commission"]))
    adjustments = Decimal(str(payload.get("adjustments", "0")))

    session.add(
        DeliveryPayout(
            platform=payload["platform"],
            payout_ref=payload["payout_ref"],
            period_start=date.fromisoformat(payload["period_start"]),
            period_end=date.fromisoformat(payload["period_end"]),
            gross_sales=gross,
            commission=commission,
            adjustments=adjustments,
            net_payout=gross - commission + adjustments,
            received_at=clock.utcnow(),
        )
    )
    session.flush()
    return {"stored": True, "net": str(gross - commission + adjustments)}


def _parse_dt(value: Any) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=clock.local_tz())
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=clock.local_tz())


HANDLERS = {
    "pos.order": handle_pos_order,
    "payment.settled": handle_payment,
    "review.posted": handle_review,
    "delivery.payout": handle_delivery_payout,
}
