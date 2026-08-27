"""Simulated point of sale.

Generates a realistic service day: two demand peaks, a channel mix, party sizes
drawn from a plausible distribution, and baskets weighted by item popularity.
The output is fed through the real webhook endpoint rather than written directly
to the database, so the simulation exercises the same ingestion path production
would.
"""

from __future__ import annotations

import random
from datetime import datetime, time

from sqlalchemy import select

from restaurant_ai import clock
from restaurant_ai.db.base import session_scope
from restaurant_ai.db.models import MenuItem
from restaurant_ai.integrations.base import PosOrder, PosOrderLine

# Share of the day's orders by hour. Two peaks, as any restaurant has.
HOURLY_SHAPE: dict[int, float] = {
    11: 0.05,
    12: 0.11,
    13: 0.09,
    14: 0.04,
    15: 0.02,
    16: 0.03,
    17: 0.05,
    18: 0.10,
    19: 0.15,
    20: 0.16,
    21: 0.11,
    22: 0.09,
}

CHANNEL_MIX: list[tuple[str, float]] = [
    ("dine_in", 0.55),
    ("takeaway", 0.15),
    ("delivery", 0.20),
    ("drive_thru", 0.06),
    ("kiosk", 0.04),
]

WEEKDAY_FACTOR = [0.72, 0.75, 0.85, 0.98, 1.35, 1.48, 1.10]
DELIVERY_PLATFORMS = ["GrabFood", "foodpanda"]

# Free-text customisations, so the order agent has real dietary requests to
# interpret rather than clean structured input.
MODIFIERS = [
    None,
    None,
    None,
    None,
    "no peanuts please",
    "extra spicy",
    "no belacan - shellfish allergy",
    "less oil",
    "no egg",
    "gluten free if possible",
    "child portion",
]


class FakePOS:
    """Deterministic POS simulator."""

    provider = "fake_pos"

    def __init__(self, base_covers: int | None = None, seed: int | None = None) -> None:
        self.base_covers = base_covers if base_covers is not None else 96
        self._seed = seed
        self._pushed: list[PosOrder] = []

    def _rng(self, business_date) -> random.Random:
        # Seeded from the date so the same day always replays identically.
        seed = self._seed if self._seed is not None else int(business_date.strftime("%Y%m%d"))
        return random.Random(seed)

    def generate_day(self, business_date=None) -> list[PosOrder]:
        """The full day's orders, in chronological order."""
        business_date = business_date or clock.today()
        rng = self._rng(business_date)

        with session_scope() as session:
            items = list(session.execute(select(MenuItem).where(MenuItem.is_active)).scalars())
            catalogue = [(i.sku, i.price, i.course, i.name) for i in items]

        if not catalogue:
            return []

        popularity = self._popularity(catalogue, rng)
        covers = int(
            self.base_covers * WEEKDAY_FACTOR[business_date.weekday()] * rng.uniform(0.9, 1.1)
        )
        order_count = max(1, covers // 2)

        orders: list[PosOrder] = []
        for index in range(order_count):
            hour = self._pick_hour(rng)
            placed = datetime.combine(
                business_date,
                time(hour, rng.randrange(0, 60), rng.randrange(0, 60)),
                tzinfo=clock.local_tz(),
            )
            channel = self._pick_channel(rng)
            party = rng.choices([1, 2, 3, 4, 5, 6], weights=[12, 38, 16, 22, 7, 5])[0]

            lines: list[PosOrderLine] = []
            for sku, price, course, _name in self._basket(catalogue, popularity, rng, party):
                quantity = 1 if course != 4 else rng.choices([1, 2], weights=[7, 3])[0]
                lines.append(
                    PosOrderLine(
                        sku=sku,
                        quantity=quantity,
                        unit_price=price,
                        course=course,
                        modifiers=rng.choice(MODIFIERS),
                    )
                )

            method = (
                "delivery_platform"
                if channel == "delivery"
                else rng.choices(["card", "ewallet", "cash"], weights=[55, 30, 15])[0]
            )
            orders.append(
                PosOrder(
                    external_id=f"POS-{business_date.strftime('%y%m%d')}-{index:05d}",
                    channel=channel,
                    placed_at=placed,
                    lines=lines,
                    party_size=party,
                    delivery_platform=(
                        rng.choice(DELIVERY_PLATFORMS) if channel == "delivery" else None
                    ),
                    payment_method=method,
                    processor_ref=(
                        f"TXN-{business_date.strftime('%y%m%d')}-{index:05d}"
                        if method != "cash"
                        else None
                    ),
                )
            )

        orders.sort(key=lambda o: o.placed_at)
        return orders

    def fetch_orders(self, since: datetime, until: datetime) -> list[PosOrder]:
        return [
            order for order in self.generate_day(since.date()) if since <= order.placed_at <= until
        ]

    def push_order(self, order: PosOrder) -> str:
        """Accept an order from the conversational agent."""
        self._pushed.append(order)
        return order.external_id or f"POS-PUSH-{len(self._pushed):05d}"

    @property
    def pushed(self) -> list[PosOrder]:
        return list(self._pushed)

    def _popularity(self, catalogue, rng: random.Random) -> dict[str, float]:
        weights: dict[str, float] = {}
        for sku, _price, course, _name in catalogue:
            base = 1.0
            if course == 4:
                base = 1.9
            elif sku in ("MNU-NASILEMK", "MNU-CHARKWAY"):
                base = 2.2
            elif course == 1:
                base = 0.75
            elif course == 3:
                base = 0.6
            weights[sku] = base * rng.uniform(0.85, 1.15)
        return weights

    def _basket(self, catalogue, popularity, rng: random.Random, party: int):
        count = min(party + rng.randrange(0, 3), 7)
        pool = list(catalogue)
        weights = [popularity[sku] for sku, *_ in pool]
        chosen = []
        for _ in range(min(count, len(pool))):
            pick = rng.choices(pool, weights=weights)[0]
            idx = pool.index(pick)
            pool.pop(idx)
            weights.pop(idx)
            chosen.append(pick)
        return chosen

    def _pick_hour(self, rng: random.Random) -> int:
        hours = list(HOURLY_SHAPE)
        return rng.choices(hours, weights=[HOURLY_SHAPE[h] for h in hours])[0]

    def _pick_channel(self, rng: random.Random) -> str:
        roll = rng.random()
        cumulative = 0.0
        for channel, share in CHANNEL_MIX:
            cumulative += share
            if roll <= cumulative:
                return channel
        return "dine_in"
