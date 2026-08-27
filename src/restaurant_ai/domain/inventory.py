"""Reorder points, safety stock and purchase order drafting.

Standard continuous-review inventory control:

    reorder point = (average daily usage x lead time) + safety stock
    safety stock  = z x sigma_daily x sqrt(lead time)

The square root on lead time is the part people usually get wrong: demand
variance accumulates over the lead time, not linearly with it. z comes from the
configured service level (1.65 == 95% chance of not stocking out during a lead
time), and is the single dial for the food-waste/stockout trade-off.

Order quantities then have to survive contact with reality: suppliers sell in
packs, enforce minimum order quantities, deliver only on certain days, and have
minimum order values. A mathematically perfect order quantity that a supplier
will not accept is worthless, so all of that is applied here.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import ROUND_CEILING, Decimal

ZERO = Decimal("0")


@dataclass
class StockPosition:
    """What the reorder calculation needs to know about one ingredient."""

    ingredient_id: str
    code: str
    name: str
    base_uom: str
    on_hand: Decimal
    on_order: Decimal = ZERO
    avg_daily_usage: Decimal = ZERO
    usage_stddev: Decimal = ZERO
    lead_time_days: int = 2
    target_days_cover: int = 7
    shelf_life_days: int = 7

    @property
    def available(self) -> Decimal:
        return self.on_hand + self.on_order


@dataclass
class SupplierPack:
    """How an ingredient is actually purchased."""

    stock_item_id: str
    supplier_id: str
    supplier_code: str
    supplier_name: str
    supplier_sku: str
    pack_size: Decimal
    pack_uom: str
    contract_price: Decimal
    min_order_qty: Decimal = Decimal("1")
    lead_time_days: int = 2
    min_order_value: Decimal = ZERO
    delivery_days: tuple[int, ...] = (0, 1, 2, 3, 4, 5, 6)


@dataclass
class ReorderSuggestion:
    ingredient_id: str
    code: str
    name: str
    on_hand: Decimal
    on_order: Decimal
    reorder_point: Decimal
    safety_stock: Decimal
    target_level: Decimal
    shortfall: Decimal
    packs_to_order: Decimal
    pack: SupplierPack
    line_cost: Decimal
    days_cover_now: Decimal
    urgency: str  # "stockout" | "critical" | "low"
    rationale: str


@dataclass
class DraftPurchaseOrder:
    supplier_id: str
    supplier_code: str
    supplier_name: str
    expected_delivery_on: date
    lines: list[ReorderSuggestion] = field(default_factory=list)
    below_minimum: bool = False
    notes: list[str] = field(default_factory=list)

    @property
    def subtotal(self) -> Decimal:
        return sum((line.line_cost for line in self.lines), ZERO).quantize(Decimal("0.01"))

    @property
    def total(self) -> Decimal:
        # Purchases of raw food are zero-rated here; tax is applied at sale.
        return self.subtotal


def safety_stock(
    usage_stddev: Decimal, lead_time_days: int, service_level_z: float = 1.65
) -> Decimal:
    """z * sigma * sqrt(lead time), the buffer against demand variability."""
    if usage_stddev <= 0 or lead_time_days <= 0:
        return ZERO
    factor = Decimal(str(service_level_z)) * Decimal(str(math.sqrt(lead_time_days)))
    return (usage_stddev * factor).quantize(Decimal("0.0001"))


def reorder_point(
    avg_daily_usage: Decimal,
    lead_time_days: int,
    usage_stddev: Decimal = ZERO,
    service_level_z: float = 1.65,
) -> Decimal:
    """Level at which an order must be placed to avoid stocking out."""
    lead_demand = avg_daily_usage * Decimal(lead_time_days)
    return (lead_demand + safety_stock(usage_stddev, lead_time_days, service_level_z)).quantize(
        Decimal("0.0001")
    )


def target_level(
    avg_daily_usage: Decimal,
    target_days_cover: int,
    lead_time_days: int,
    usage_stddev: Decimal = ZERO,
    service_level_z: float = 1.65,
    shelf_life_days: int | None = None,
) -> Decimal:
    """Order-up-to level.

    Capped by shelf life: ordering ten days of cover for something that spoils in
    three just moves money from the bank into the bin.
    """
    days = target_days_cover
    if shelf_life_days is not None:
        days = min(days, max(shelf_life_days - 1, 1))
    return (
        avg_daily_usage * Decimal(days + lead_time_days)
        + safety_stock(usage_stddev, lead_time_days, service_level_z)
    ).quantize(Decimal("0.0001"))


def days_of_cover(on_hand: Decimal, avg_daily_usage: Decimal) -> Decimal:
    if avg_daily_usage <= 0:
        return Decimal("999")
    return (on_hand / avg_daily_usage).quantize(Decimal("0.1"))


def evaluate_position(
    position: StockPosition, pack: SupplierPack, service_level_z: float = 1.65
) -> ReorderSuggestion | None:
    """Decide whether one ingredient needs ordering, and how much.

    Returns None when stock is comfortably above the reorder point.
    """
    lead = pack.lead_time_days or position.lead_time_days
    rop = reorder_point(position.avg_daily_usage, lead, position.usage_stddev, service_level_z)
    ss = safety_stock(position.usage_stddev, lead, service_level_z)

    if position.available > rop:
        return None

    target = target_level(
        position.avg_daily_usage,
        position.target_days_cover,
        lead,
        position.usage_stddev,
        service_level_z,
        position.shelf_life_days,
    )
    shortfall = target - position.available
    if shortfall <= 0:
        return None

    if pack.pack_size <= 0:
        return None
    packs = (shortfall / pack.pack_size).quantize(Decimal("1"), rounding=ROUND_CEILING)
    packs = max(packs, pack.min_order_qty)

    cover = days_of_cover(position.on_hand, position.avg_daily_usage)
    # Cover shorter than the lead time means the shelf empties before the next
    # delivery can land, whatever the safety stock says.
    if position.on_hand <= 0:
        urgency = "stockout"
    elif cover < Decimal(lead) or position.available <= ss:
        urgency = "critical"
    else:
        urgency = "low"

    effective_days = position.target_days_cover
    capped = ""
    if position.shelf_life_days < position.target_days_cover:
        effective_days = max(position.shelf_life_days - 1, 1)
        capped = (
            f", capped from {position.target_days_cover}d by {position.shelf_life_days}d shelf life"
        )

    rationale = (
        f"{position.name}: {position.on_hand:.0f}{position.base_uom} on hand "
        f"({cover} days cover) is at or below the reorder point of {rop:.0f}. "
        f"Ordering {packs} x {pack.pack_size:.0f}{position.base_uom} pack "
        f"to reach {target:.0f} ({effective_days}d cover + {lead}d lead time{capped})."
    )

    return ReorderSuggestion(
        ingredient_id=position.ingredient_id,
        code=position.code,
        name=position.name,
        on_hand=position.on_hand,
        on_order=position.on_order,
        reorder_point=rop,
        safety_stock=ss,
        target_level=target,
        shortfall=shortfall.quantize(Decimal("0.0001")),
        packs_to_order=packs,
        pack=pack,
        line_cost=(packs * pack.contract_price).quantize(Decimal("0.01")),
        days_cover_now=cover,
        urgency=urgency,
        rationale=rationale,
    )


def next_delivery_date(
    from_date: date, lead_time_days: int, delivery_days: tuple[int, ...]
) -> date:
    """Earliest date that satisfies both lead time and the supplier's delivery days."""
    candidate = from_date + timedelta(days=max(lead_time_days, 1))
    if not delivery_days:
        return candidate
    for offset in range(0, 14):
        day = candidate + timedelta(days=offset)
        if day.weekday() in delivery_days:
            return day
    return candidate


def build_purchase_orders(
    suggestions: list[ReorderSuggestion], order_date: date
) -> list[DraftPurchaseOrder]:
    """Group suggestions into one draft PO per supplier.

    A supplier whose total falls under their minimum order value is still
    returned, flagged, with a note — suppressing it silently would hide a real
    stockout risk from the human approving the batch.
    """
    grouped: dict[str, list[ReorderSuggestion]] = {}
    for suggestion in suggestions:
        grouped.setdefault(suggestion.pack.supplier_id, []).append(suggestion)

    drafts: list[DraftPurchaseOrder] = []
    for supplier_id, lines in grouped.items():
        pack = lines[0].pack
        draft = DraftPurchaseOrder(
            supplier_id=supplier_id,
            supplier_code=pack.supplier_code,
            supplier_name=pack.supplier_name,
            expected_delivery_on=next_delivery_date(
                order_date, pack.lead_time_days, pack.delivery_days
            ),
            lines=sorted(lines, key=lambda line: line.line_cost, reverse=True),
        )
        if pack.min_order_value > 0 and draft.subtotal < pack.min_order_value:
            draft.below_minimum = True
            draft.notes.append(
                f"Subtotal {draft.subtotal} is below {pack.supplier_name}'s minimum order "
                f"value of {pack.min_order_value}. Consider consolidating with the next "
                f"delivery day or topping up slow-moving lines."
            )
        urgent = [line for line in draft.lines if line.urgency == "stockout"]
        if urgent:
            draft.notes.append(
                f"{len(urgent)} line(s) are already at zero on hand: "
                + ", ".join(line.name for line in urgent[:5])
            )
        drafts.append(draft)

    return sorted(drafts, key=lambda d: d.subtotal, reverse=True)


def recalculate_usage(
    daily_usage: list[Decimal],
) -> tuple[Decimal, Decimal]:
    """Mean and standard deviation of daily usage, for refreshing a reorder policy."""
    from restaurant_ai.domain.forecasting import _mean, stdev

    if not daily_usage:
        return ZERO, ZERO
    return _mean(daily_usage), stdev(daily_usage)
