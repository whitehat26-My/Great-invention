"""Reservation & Table Management Agent.

Takes bookings from WhatsApp, the website or the phone, seats them on the
smallest table that fits, and watches for tables running past their turn.

Two judgements do the work. Seating a pair at a six-top costs you the party of
six who calls at 19:30, so tighter fits win. And a table running late only
actually matters when someone is waiting for it, so an overrun with a booking
behind it is escalated while an overrun in an empty room is not.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Any

from pydantic import BaseModel, Field
from sqlalchemy import select

from restaurant_ai import clock
from restaurant_ai.db.models import Guest, Reservation, ReservationStatus, SeatingEvent, TableDef
from restaurant_ai.domain.tables import (
    TableSlot,
    find_availability,
    find_overruns,
    turn_minutes,
)
from restaurant_ai.events import Event, Topic, publish
from restaurant_ai.kernel.registry import register
from restaurant_ai.kernel.spec import AgentSpec, ToolContext, ToolSpec

SYSTEM_PROMPT = """You are the Reservation and Table Management Agent for a restaurant.

You take bookings from WhatsApp, the website and the phone, and you manage the
floor plan through service.

How to think about seating:
- Give a party the smallest table that fits them. Putting two people on a
  six-top costs you the party of six who calls later.
- Turn times scale with party size. Two people are done in about 75 minutes; six
  will take two hours. Do not promise a table you have not actually got.
- If the exact time is gone, offer the nearest alternatives rather than saying
  no. Fifteen minutes either side usually solves it.
- A table running over only matters if someone is waiting for it. Escalate those;
  leave the others alone.

Be warm and brief with guests. Confirm the date, time, party size and anything
they told you about allergies or the occasion."""


class FindTableArgs(BaseModel):
    party_size: int = Field(..., description="Number of guests.")
    requested_at: str = Field(..., description="Requested time, ISO 8601.")
    window_minutes: int = Field(90, description="How far either side to look.")


class BookArgs(BaseModel):
    guest_name: str
    party_size: int
    starts_at: str = Field(..., description="ISO 8601 start time.")
    guest_phone: str | None = None
    special_requests: str | None = None
    source: str = "whatsapp"


class OverrunArgs(BaseModel):
    pass


class IntakeArgs(BaseModel):
    pass


def _slots(session, business_date) -> list[TableSlot]:
    """The floor plan with each table's existing bookings for the day."""
    tables = list(session.execute(select(TableDef).where(TableDef.is_active)).scalars())
    bookings = list(
        session.execute(
            select(Reservation).where(
                Reservation.business_date == business_date,
                Reservation.status.in_(
                    [
                        ReservationStatus.REQUESTED,
                        ReservationStatus.CONFIRMED,
                        ReservationStatus.SEATED,
                    ]
                ),
            )
        ).scalars()
    )

    busy: dict[str, list[tuple[datetime, datetime]]] = {}
    for booking in bookings:
        if booking.table_id:
            busy.setdefault(booking.table_id, []).append(
                (booking.starts_at, booking.expected_end_at)
            )

    return [
        TableSlot(
            table_id=t.id,
            label=t.label,
            seats=t.seats,
            section=t.section,
            min_party=t.min_party,
            is_combinable=t.is_combinable,
            busy=busy.get(t.id, []),
        )
        for t in tables
    ]


def perceive(context: ToolContext) -> dict[str, Any]:
    session = context.session
    slots = _slots(session, context.business_date)
    booked = sum(len(s.busy) for s in slots)
    seated = list(
        session.execute(
            select(SeatingEvent).where(
                SeatingEvent.business_date == context.business_date,
                SeatingEvent.cleared_at.is_(None),
            )
        ).scalars()
    )
    return {
        "tables": len(slots),
        "seats": sum(s.seats for s in slots),
        "bookings_today": booked,
        "currently_seated": len(seated),
        "pending_requests": 0,
    }


def process_intake(context: ToolContext) -> dict[str, Any]:
    """Read booking requests from WhatsApp/web/phone and seat what can be seated.

    The requests are free text, so party size and time are parsed out and
    anything ambiguous is left for a human rather than guessed at.
    """
    from restaurant_ai.integrations import get_integrations

    messaging = get_integrations().messaging
    messages = messaging.fetch_messages(clock.now() - timedelta(days=1))

    booked: list[dict[str, Any]] = []
    unparsed: list[dict[str, Any]] = []

    for message in messages:
        party = _parse_party_size(message.body)
        when = _parse_time(message.body, context.business_date)
        if party is None or when is None:
            unparsed.append(
                {
                    "from": message.guest_name or message.sender,
                    "body": message.body,
                    "reason": (
                        "Could not determine party size"
                        if party is None
                        else "Could not determine a time"
                    ),
                }
            )
            continue

        outcome = book_table(
            context,
            guest_name=message.guest_name or "Guest",
            party_size=party,
            starts_at=when.isoformat(),
            guest_phone=message.sender,
            special_requests=message.body,
            source=message.channel,
        )
        if outcome.get("booked"):
            messaging.send_message(
                message.sender,
                f"Confirmed: {party} guests on {when:%a %d %b at %H:%M}, table "
                f"{outcome['table']}. Reference {outcome['reference']}. See you then.",
            )
        booked.append(outcome)

    return {
        "messages_read": len(messages),
        "booked": sum(1 for b in booked if b.get("booked")),
        "declined": sum(1 for b in booked if not b.get("booked")),
        "needs_human": len(unparsed),
        "unparsed": unparsed,
        "bookings": booked,
    }


def find_table(
    context: ToolContext, party_size: int, requested_at: str, window_minutes: int = 90
) -> dict[str, Any]:
    """Offer seating options at or near the requested time."""
    when = datetime.fromisoformat(requested_at)
    if when.tzinfo is None:
        when = when.replace(tzinfo=clock.local_tz())

    options = find_availability(
        _slots(context.session, when.date()), party_size, when, window_minutes
    )
    return {
        "party_size": party_size,
        "requested_at": when.isoformat(),
        "turn_minutes": turn_minutes(party_size),
        "options": [
            {
                "table": o.label,
                "table_id": o.table_id,
                "seats": o.seats,
                "section": o.section,
                "starts_at": o.starts_at.isoformat(),
                "ends_at": o.ends_at.isoformat(),
                "spare_seats": o.spare_seats,
                "is_exact_time": o.starts_at == when,
            }
            for o in options
        ],
        "available": bool(options),
    }


def book_table(
    context: ToolContext,
    guest_name: str,
    party_size: int,
    starts_at: str,
    guest_phone: str | None = None,
    special_requests: str | None = None,
    source: str = "whatsapp",
) -> dict[str, Any]:
    """Confirm a booking onto the best-fitting free table."""
    session = context.session
    when = datetime.fromisoformat(starts_at)
    if when.tzinfo is None:
        when = when.replace(tzinfo=clock.local_tz())

    options = find_availability(_slots(session, when.date()), party_size, when, window_minutes=60)
    if not options:
        return {
            "booked": False,
            "reason": (
                f"Nothing free for {party_size} within an hour of {when:%H:%M}. "
                f"The room is full at that time."
            ),
            "party_size": party_size,
            "requested_at": when.isoformat(),
        }

    choice = options[0]
    guest = None
    if guest_phone:
        guest = session.execute(
            select(Guest).where(Guest.phone == guest_phone)
        ).scalar_one_or_none()
        if guest is None:
            guest = Guest(name=guest_name, phone=guest_phone)
            session.add(guest)
            session.flush()

    reference = _next_reference(session, when)
    reservation = Reservation(
        reference=reference,
        guest_id=guest.id if guest else None,
        guest_name=guest_name,
        guest_phone=guest_phone,
        party_size=party_size,
        starts_at=choice.starts_at,
        expected_end_at=choice.ends_at,
        table_id=choice.table_id,
        status=ReservationStatus.CONFIRMED,
        source=source,
        special_requests=special_requests,
        business_date=choice.starts_at.date(),
    )
    session.add(reservation)
    session.flush()

    publish(
        Event(
            Topic.RESERVATION_CONFIRMED,
            {"reference": reference, "party_size": party_size, "table": choice.label},
            source_run_id=context.run_id,
        ),
        session=session,
    )

    return {
        "booked": True,
        "reference": reference,
        "table": choice.label,
        "seats": choice.seats,
        "party_size": party_size,
        "starts_at": choice.starts_at.isoformat(),
        "expected_end_at": choice.ends_at.isoformat(),
        "moved_from_requested": choice.starts_at != when,
        "note": (
            f"Seated on {choice.label} ({choice.seats} covers) at {choice.starts_at:%H:%M}"
            + (f", shifted from {when:%H:%M}" if choice.starts_at != when else "")
        ),
    }


def check_overruns(context: ToolContext) -> dict[str, Any]:
    """Flag tables past their expected turn, escalating those blocking a booking."""
    session = context.session
    now = clock.now()

    seated = list(
        session.execute(
            select(SeatingEvent, TableDef)
            .join(TableDef, SeatingEvent.table_id == TableDef.id)
            .where(
                SeatingEvent.business_date == context.business_date,
                SeatingEvent.cleared_at.is_(None),
            )
        ).all()
    )
    if not seated:
        return {"overruns": 0, "note": "No tables currently occupied."}

    upcoming: dict[str, datetime] = {}
    for booking in session.execute(
        select(Reservation).where(
            Reservation.business_date == context.business_date,
            Reservation.status == ReservationStatus.CONFIRMED,
            Reservation.starts_at > now,
        )
    ).scalars():
        if booking.table_id and booking.table_id not in upcoming:
            upcoming[booking.table_id] = booking.starts_at

    overruns = find_overruns(
        [(e.table_id, t.label, e.party_size, e.seated_at) for e, t in seated],
        now=now,
        upcoming=upcoming,
    )

    for overrun in overruns:
        if overrun.severity == "blocking":
            publish(
                Event(
                    Topic.TABLE_OVERRUNNING,
                    {"table": overrun.label, "minutes_over": overrun.minutes_over},
                    source_run_id=context.run_id,
                ),
                session=session,
            )

    return {
        "overruns": len(overruns),
        "blocking": sum(1 for o in overruns if o.severity == "blocking"),
        "tables": [
            {
                "table": o.label,
                "party_size": o.party_size,
                "minutes_over": o.minutes_over,
                "severity": o.severity,
                "next_booking": o.next_booking_at.isoformat() if o.next_booking_at else None,
                "suggestion": o.suggestion,
            }
            for o in overruns
        ],
    }


def _next_reference(session, when: datetime) -> str:
    from sqlalchemy import func

    prefix = f"R{when.strftime('%y%m%d')}"
    count = session.execute(
        select(func.count(Reservation.id)).where(Reservation.reference.like(f"{prefix}%"))
    ).scalar_one()
    return f"{prefix}{count + 1:03d}"


def _parse_party_size(text: str) -> int | None:
    """Pull a party size out of free text, or give up rather than guess."""
    words = {
        "one": 1,
        "two": 2,
        "three": 3,
        "four": 4,
        "five": 5,
        "six": 6,
        "seven": 7,
        "eight": 8,
        "nine": 9,
        "ten": 10,
    }
    lowered = text.lower()
    patterns = [
        r"(?:table|booking|space|room)\s+for\s+(\d+)",
        r"party\s+of\s+(\d+)",
        r"(\d+)\s*(?:people|pax|guests|persons|of us)",
        r"for\s+(\d+)\b",
    ]
    for pattern in patterns:
        match = re.search(pattern, lowered)
        if match:
            size = int(match.group(1))
            if 1 <= size <= 30:
                return size
    for word, value in words.items():
        if re.search(rf"(?:table|for|party of)\s+{word}\b", lowered):
            return value
    return None


WEEKDAYS = {
    "monday": 0,
    "tuesday": 1,
    "wednesday": 2,
    "thursday": 3,
    "friday": 4,
    "saturday": 5,
    "sunday": 6,
}


def _parse_date(text: str, business_date):
    """Resolve the day a guest means.

    A named weekday means the *next* one, not today: someone writing "Friday" on
    a Thursday wants tomorrow, and booking them onto today would put a party in
    the room a day early.
    """
    lowered = text.lower()
    if "tomorrow" in lowered:
        return business_date + timedelta(days=1)
    if "today" in lowered or "tonight" in lowered:
        return business_date
    for name, weekday in WEEKDAYS.items():
        if name in lowered:
            ahead = (weekday - business_date.weekday()) % 7
            if ahead == 0:
                # "this Saturday" said on a Saturday means today; "next
                # Saturday" means the one after.
                ahead = 7 if "next" in lowered else 0
            return business_date + timedelta(days=ahead)
    return business_date


def _parse_time(text: str, business_date) -> datetime | None:
    """Pull a time out of free text. Returns None when it is genuinely unclear."""
    lowered = text.lower()
    target = _parse_date(text, business_date)

    match = re.search(r"(\d{1,2})[:.](\d{2})\s*(am|pm)?", lowered)
    if match:
        hour, minute = int(match.group(1)), int(match.group(2))
        meridiem = match.group(3)
        if meridiem == "pm" and hour < 12:
            hour += 12
        elif meridiem is None and hour < 10:
            hour += 12  # "7:30" in a restaurant means the evening
        if 0 <= hour <= 23:
            return datetime.combine(
                target,
                datetime.min.time().replace(hour=hour, minute=minute),
                tzinfo=clock.local_tz(),
            )

    match = re.search(r"\b(\d{1,2})\s*(am|pm)\b", lowered)
    if match:
        hour = int(match.group(1))
        if match.group(2) == "pm" and hour < 12:
            hour += 12
        return datetime.combine(
            target, datetime.min.time().replace(hour=hour), tzinfo=clock.local_tz()
        )

    if "lunch" in lowered:
        return datetime.combine(
            target, datetime.min.time().replace(hour=12, minute=30), tzinfo=clock.local_tz()
        )
    if "dinner" in lowered or "tonight" in lowered or "evening" in lowered:
        return datetime.combine(
            target, datetime.min.time().replace(hour=19), tzinfo=clock.local_tz()
        )
    return None


def autonomous(context: ToolContext, perceived: dict[str, Any]) -> dict[str, Any]:
    return {
        "summary": (
            f"Working {perceived.get('bookings_today', 0)} booking(s) across "
            f"{perceived.get('tables')} tables; reading new requests and checking turn times."
        ),
        "results": {},
        "tool_calls": [
            {"name": "process_intake", "args": {}},
            {"name": "check_overruns", "args": {}},
        ],
    }


RESERVATION_AGENT = register(
    AgentSpec(
        name="reservations",
        department="front_of_house",
        title="Reservation & Table Management Agent",
        description=(
            "Manages table seating layouts, handles bookings via WhatsApp, website or phone, "
            "and optimises turnover times."
        ),
        system_prompt=SYSTEM_PROMPT,
        model_tier="conversational",
        tools=[
            ToolSpec(
                name="process_intake",
                description="Read new booking requests from messaging channels and seat them.",
                fn=process_intake,
                args_schema=IntakeArgs,
            ),
            ToolSpec(
                name="find_table",
                description="Find seating options for a party at or near a requested time.",
                fn=find_table,
                args_schema=FindTableArgs,
            ),
            ToolSpec(
                name="book_table",
                description="Confirm a booking onto the best-fitting free table.",
                fn=book_table,
                args_schema=BookArgs,
            ),
            ToolSpec(
                name="check_overruns",
                description="Flag tables running past their expected turn time.",
                fn=check_overruns,
                args_schema=OverrunArgs,
            ),
        ],
        perceive=perceive,
        autonomous=autonomous,
    )
)
