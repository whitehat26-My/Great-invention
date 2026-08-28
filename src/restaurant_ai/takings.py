"""Recording a day's trading by hand.

Every number the platform produces describes a fiction until real sales go in,
and the usual way in is a POS export. A restaurant that is still being built has
no POS to export from — and waiting for one means the system stays a
demonstration for however many months that takes.

This is the way in that needs nothing: a message from the owner's phone saying
what sold.

    /sold 20 nasi lemak biasa, 35 teh tarik, 40 roti kosong

Three decisions shape it:

- **A name is matched, never guessed.** "nasi lemak" matches eight dishes on
  this menu, and picking one would put revenue against a dish that did not sell.
  Ambiguity is reported with the candidates; nothing is written until every line
  resolves to exactly one dish.
- **Nothing is written until the owner confirms.** The reply says what it
  understood and what it will record, in money, before it records anything.
  A typo caught on screen costs a re-type; a typo written into the books costs
  an evening finding it.
- **These are real orders.** They carry no ``H`` prefix, so they count as real
  trading everywhere the demo data is excluded, and they deduct stock and drive
  margin exactly as a POS-fed order would.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from restaurant_ai import clock
from restaurant_ai.db.models import MenuItem, OrderChannel, OrderHeader, OrderLine, OrderStatus
from restaurant_ai.logging_setup import get_logger

log = get_logger(__name__)

# "20 nasi lemak" and "nasi lemak 20" are both how people write it down.
_LEADING_QTY = re.compile(r"^\s*(\d+)\s*[x×]?\s+(.*\S)\s*$", re.IGNORECASE)
_TRAILING_QTY = re.compile(r"^\s*(.*\S)\s+[x×]?\s*(\d+)\s*$", re.IGNORECASE)


@dataclass
class Entry:
    """One line of the owner's message, and what it resolved to."""

    text: str
    quantity: int
    item: MenuItem | None = None
    candidates: list[str] = field(default_factory=list)

    @property
    def resolved(self) -> bool:
        return self.item is not None

    @property
    def value(self) -> Decimal:
        if self.item is None:
            return Decimal("0")
        return Decimal(self.quantity) * self.item.price


@dataclass
class Reading:
    """What a whole message came to."""

    entries: list[Entry] = field(default_factory=list)
    covers: int | None = None

    @property
    def resolved(self) -> list[Entry]:
        return [e for e in self.entries if e.resolved]

    @property
    def unresolved(self) -> list[Entry]:
        return [e for e in self.entries if not e.resolved]

    @property
    def total(self) -> Decimal:
        return sum((e.value for e in self.resolved), Decimal("0"))

    @property
    def usable(self) -> bool:
        return bool(self.resolved) and not self.unresolved


def split_entries(text: str) -> list[tuple[str, int]]:
    """Pull "name × quantity" pairs out of one line of plain writing.

    Commas separate dishes; "and" too, because people write both. A fragment
    with no number is a dish somebody forgot to count, and is returned with a
    quantity of zero rather than silently assumed to be one — one is a guess
    about money.
    """
    found: list[tuple[str, int]] = []
    for fragment in re.split(r",|\band\b|\n|;", text):
        fragment = fragment.strip(" .\t")
        if not fragment:
            continue
        leading = _LEADING_QTY.match(fragment)
        trailing = _TRAILING_QTY.match(fragment)
        if leading:
            found.append((leading.group(2), int(leading.group(1))))
        elif trailing:
            found.append((trailing.group(1), int(trailing.group(2))))
        else:
            found.append((fragment, 0))
    return found


def match_dish(session: Session, name: str) -> tuple[MenuItem | None, list[str]]:
    """Find the one dish this names, or report what it could have been.

    Exact first, then a whole-word containment. Never a "closest" match: on a
    menu with eight Nasi Lemaks, closest is a coin-flip that puts money against
    a dish that did not sell.
    """
    cleaned = " ".join(name.lower().split())
    if not cleaned:
        return None, []

    items = list(session.execute(select(MenuItem).where(MenuItem.is_active)).scalars())

    exact = [i for i in items if i.name.lower() == cleaned]
    if len(exact) == 1:
        return exact[0], []

    contains = [i for i in items if cleaned in i.name.lower()]
    if len(contains) == 1:
        return contains[0], []
    if contains:
        return None, sorted(i.name for i in contains)[:8]

    # Last resort: every word the owner typed appears somewhere in the name.
    words = cleaned.split()
    loose = [i for i in items if all(w in i.name.lower() for w in words)]
    if len(loose) == 1:
        return loose[0], []
    return None, sorted(i.name for i in loose)[:8]


def read(session: Session, text: str) -> Reading:
    """Understand a message without writing anything."""
    reading = Reading()

    covers = re.search(r"(\d+)\s*covers?\b", text, re.IGNORECASE)
    if covers:
        reading.covers = int(covers.group(1))
        text = text[: covers.start()] + text[covers.end() :]

    for name, quantity in split_entries(text):
        entry = Entry(text=name, quantity=quantity)
        if quantity > 0:
            entry.item, entry.candidates = match_dish(session, name)
        reading.entries.append(entry)
    return reading


def record(session: Session, reading: Reading, business_date: date | None = None) -> dict:
    """Write the day's sales as real orders.

    Refuses a reading that did not fully resolve: a partial write would leave
    the owner unsure which half went in, and re-sending the whole message would
    double the half that did.
    """
    if not reading.usable:
        raise ValueError("every line must resolve to one dish before anything is recorded")

    day = business_date or clock.today()
    # Never the seed's `H` prefix: these are real trading and must count as it.
    taken = session.execute(
        select(func.count())
        .select_from(OrderHeader)
        .where(OrderHeader.business_date == day, OrderHeader.order_number.like("MAN-%"))
    ).scalar_one()

    order = OrderHeader(
        order_number=f"MAN-{day:%y%m%d}-{taken + 1:03d}",
        channel=OrderChannel.DINE_IN,
        status=OrderStatus.CLOSED,
        party_size=reading.covers or sum(e.quantity for e in reading.resolved),
        placed_at=clock.now(),
        closed_at=clock.now(),
        business_date=day,
        subtotal=reading.total,
        discount=Decimal("0"),
        tax=Decimal("0"),
        total=reading.total,
        notes="Recorded by hand from Telegram.",
    )
    session.add(order)
    session.flush()

    for entry in reading.resolved:
        assert entry.item is not None
        session.add(
            OrderLine(
                order_id=order.id,
                menu_item_id=entry.item.id,
                quantity=Decimal(entry.quantity),
                unit_price=entry.item.price,
                line_total=entry.value,
                course=entry.item.course,
            )
        )
    session.flush()

    log.info(
        "takings recorded by hand",
        order=order.order_number,
        lines=len(reading.resolved),
        total=str(reading.total),
    )
    return {
        "order_number": order.order_number,
        "business_date": day.isoformat(),
        "lines": len(reading.resolved),
        "covers": order.party_size,
        "total": str(reading.total),
    }
