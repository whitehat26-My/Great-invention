"""Telling the demo restaurant from the real one.

``restaurant-ai seed`` invents a fortnight of trading — a couple of thousand
orders, with covers, revenue and a plausible prime cost. That is exactly right
for proving the platform works, and exactly wrong to read as fact: Camelia
closes the day on it and the brief arrives on a phone reporting the takings of
a restaurant that does not exist.

The danger is not today, when the owner has just typed `seed` and remembers.
It is the week the real restaurant opens, when real orders start landing beside
the invented ones and every total quietly mixes the two.

Seeded orders are numbered ``H<yymmdd>-<counter>`` — H for historical — which
the seed has always done and nothing else does. That is the whole mechanism:
countable, and unmistakable.
"""

from __future__ import annotations

from datetime import date

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from restaurant_ai.db.models import OrderHeader

# The prefix `seed` gives every order it invents.
SYNTHETIC_PREFIX = "H"


def synthetic_orders(session: Session, business_date: date | None = None) -> int:
    """How many orders here were invented rather than taken."""
    query = (
        select(func.count())
        .select_from(OrderHeader)
        .where(OrderHeader.order_number.like(f"{SYNTHETIC_PREFIX}%"))
    )
    if business_date is not None:
        query = query.where(OrderHeader.business_date == business_date)
    return int(session.execute(query).scalar_one())


def real_orders(session: Session, business_date: date | None = None) -> int:
    """How many were actually taken from a guest."""
    query = (
        select(func.count())
        .select_from(OrderHeader)
        .where(OrderHeader.order_number.notlike(f"{SYNTHETIC_PREFIX}%"))
    )
    if business_date is not None:
        query = query.where(OrderHeader.business_date == business_date)
    return int(session.execute(query).scalar_one())


def describe(session: Session, business_date: date | None = None) -> str | None:
    """One line when demo data is in the numbers, None when it is not.

    None is the important half: once the restaurant is real this says nothing
    at all, rather than becoming a banner everyone learns to skip past.
    """
    invented = synthetic_orders(session, business_date)
    if not invented:
        return None
    genuine = real_orders(session, business_date)
    if genuine:
        return (
            f"{invented} of these orders are demo data from `seed`, "
            f"{genuine} are real — the totals mix both"
        )
    return f"all {invented} orders here are demo data from `seed`, not real trading"
