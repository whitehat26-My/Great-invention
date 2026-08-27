"""Unit conversion.

Conversions are resolved in three steps: an identity match, a global table of
mass/volume/count factors, then any ingredient-specific override recorded in
``uom_conversion`` (for example, one egg weighing 50 g).
"""

from __future__ import annotations

from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from restaurant_ai.db.models import UomConversion


class UomError(ValueError):
    """Raised when two units cannot be reconciled."""


# Canonical base unit per dimension, and the factor to reach it.
_TO_BASE: dict[str, tuple[str, Decimal]] = {
    # mass -> g
    "g": ("g", Decimal("1")),
    "gram": ("g", Decimal("1")),
    "grams": ("g", Decimal("1")),
    "kg": ("g", Decimal("1000")),
    "mg": ("g", Decimal("0.001")),
    # volume -> ml
    "ml": ("ml", Decimal("1")),
    "l": ("ml", Decimal("1000")),
    "litre": ("ml", Decimal("1000")),
    "cl": ("ml", Decimal("10")),
    # count -> ea
    "ea": ("ea", Decimal("1")),
    "each": ("ea", Decimal("1")),
    "unit": ("ea", Decimal("1")),
    "pc": ("ea", Decimal("1")),
    "portion": ("ea", Decimal("1")),
}


def normalise(uom: str) -> str:
    return (uom or "").strip().lower()


def dimension_of(uom: str) -> str | None:
    entry = _TO_BASE.get(normalise(uom))
    return entry[0] if entry else None


def convert(
    quantity: Decimal,
    from_uom: str,
    to_uom: str,
    session: Session | None = None,
    ingredient_id: str | None = None,
) -> Decimal:
    """Convert ``quantity`` from one unit to another.

    Falls back to the ``uom_conversion`` table when the units belong to
    different dimensions (mass vs count, say), which is where pack-level and
    ingredient-specific conversions live.
    """
    src, dst = normalise(from_uom), normalise(to_uom)
    if src == dst:
        return quantity

    src_entry, dst_entry = _TO_BASE.get(src), _TO_BASE.get(dst)
    if src_entry and dst_entry and src_entry[0] == dst_entry[0]:
        return quantity * src_entry[1] / dst_entry[1]

    if session is not None:
        factor = _lookup_factor(session, src, dst, ingredient_id)
        if factor is not None:
            return quantity * factor

    raise UomError(
        f"Cannot convert {from_uom!r} to {to_uom!r}"
        + (f" for ingredient {ingredient_id}" if ingredient_id else "")
        + ". Add a uom_conversion row to define this relationship."
    )


def _lookup_factor(
    session: Session, src: str, dst: str, ingredient_id: str | None
) -> Decimal | None:
    """Ingredient-specific rows win over global ones, in either direction."""
    stmt = select(UomConversion).where(UomConversion.from_uom == src, UomConversion.to_uom == dst)
    rows = list(session.execute(stmt).scalars())
    for row in sorted(rows, key=lambda r: r.ingredient_id is None):
        if row.ingredient_id in (None, ingredient_id):
            return row.factor

    reverse = select(UomConversion).where(
        UomConversion.from_uom == dst, UomConversion.to_uom == src
    )
    rows = list(session.execute(reverse).scalars())
    for row in sorted(rows, key=lambda r: r.ingredient_id is None):
        if row.ingredient_id in (None, ingredient_id) and row.factor != 0:
            return Decimal("1") / row.factor
    return None


def compatible(from_uom: str, to_uom: str) -> bool:
    """True when the two units convert without consulting the database."""
    src, dst = normalise(from_uom), normalise(to_uom)
    if src == dst:
        return True
    a, b = _TO_BASE.get(src), _TO_BASE.get(dst)
    return bool(a and b and a[0] == b[0])
