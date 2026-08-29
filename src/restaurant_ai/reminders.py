"""The owner's own diary.

Everything else in this platform is work an agent does. This is the one place it
holds work a *person* has to do, and Aziera's job is to make sure they do it.

A restaurant that has traded twenty years runs on things nobody wrote down. The
halal certificate expires in March, the extinguisher service is due, the landlord
wants an answer by Friday. They are carried in someone's head and remembered
every week except the week that matters, and a lapsed licence closes the door.

Two rules shape it:

- **Chase, but not every day identically.** A reminder repeated verbatim each
  morning is one the owner learns to skip, which is the same as not having it.
  Something is raised as it approaches, then once a day at most, and the record
  of when it was last raised is what enforces that.
- **Overdue is never dropped.** A date that has passed is the reminder working
  hardest, not one that has expired. Nothing here disappears by going stale;
  it disappears when a person says it is done.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from restaurant_ai import clock
from restaurant_ai.db.models import Reminder
from restaurant_ai.logging_setup import get_logger

log = get_logger(__name__)

# How far ahead something starts being mentioned. A week is long enough to act
# on a renewal that needs posting and short enough not to be noise.
HORIZON = timedelta(days=7)


@dataclass
class Due:
    """One thing that wants the owner's attention, and how urgently."""

    id: str
    what: str
    due_on: date
    detail: str | None
    days_left: int

    @property
    def overdue(self) -> bool:
        return self.days_left < 0

    def phrase(self) -> str:
        """How a person would say it, rather than a date and a number."""
        if self.days_left < 0:
            days = -self.days_left
            return f"{self.what} — was due {days} day{'s' if days != 1 else ''} ago"
        if self.days_left == 0:
            return f"{self.what} — today"
        if self.days_left == 1:
            return f"{self.what} — tomorrow"
        return f"{self.what} — in {self.days_left} days ({self.due_on:%-d %b})"


def add(
    session: Session,
    what: str,
    due_on: date,
    detail: str | None = None,
    raised_by: str = "owner",
) -> Reminder:
    """Write something down so it stops living in somebody's head."""
    reminder = Reminder(
        what=what.strip()[:300],
        due_on=due_on,
        detail=(detail or None),
        raised_by=raised_by,
    )
    session.add(reminder)
    session.flush()
    log.info("reminder added", what=reminder.what, due=due_on.isoformat())
    return reminder


def due(session: Session, within: timedelta = HORIZON, on: date | None = None) -> list[Due]:
    """What is coming up or already late, soonest first.

    Overdue items come first because they are the ones that have already cost
    something, and no window excludes them — a date that has passed is the
    reminder working hardest, not one that has expired.
    """
    today = on or clock.today()
    rows = list(
        session.execute(
            select(Reminder)
            .where(Reminder.done_at.is_(None), Reminder.due_on <= today + within)
            .order_by(Reminder.due_on)
        ).scalars()
    )
    return [
        Due(
            id=row.id,
            what=row.what,
            due_on=row.due_on,
            detail=row.detail,
            days_left=(row.due_on - today).days,
        )
        for row in rows
    ]


def open_items(session: Session) -> list[Due]:
    """Everything outstanding, however far off."""
    return due(session, within=timedelta(days=3650))


def complete(session: Session, reminder_id: str) -> Reminder | None:
    """Mark it done. Kept rather than deleted: "did I renew it last year?" is
    a question asked afterwards, by the owner or by an inspector."""
    row = session.get(Reminder, reminder_id)
    if row is None or row.done_at is not None:
        return None
    row.done_at = clock.now()
    session.flush()
    log.info("reminder completed", what=row.what)
    return row


def find(session: Session, text: str) -> list[Reminder]:
    """Open reminders whose wording contains this, for "the halal thing is done"."""
    needle = " ".join((text or "").lower().split())
    if not needle:
        return []
    rows = session.execute(select(Reminder).where(Reminder.done_at.is_(None))).scalars()
    return [row for row in rows if needle in row.what.lower()]


def mark_raised(session: Session, ids: list[str], on: date | None = None) -> None:
    """Record that these were mentioned today.

    A reminder repeated verbatim every morning is one the owner learns to skip,
    which is the same as not having it at all.
    """
    today = on or clock.today()
    for reminder_id in ids:
        row = session.get(Reminder, reminder_id)
        if row is not None:
            row.last_raised_on = today


def unraised_today(session: Session, items: list[Due], on: date | None = None) -> list[Due]:
    """Of these, the ones not already mentioned today."""
    today = on or clock.today()
    fresh = []
    for item in items:
        row = session.get(Reminder, item.id)
        if row is not None and row.last_raised_on != today:
            fresh.append(item)
    return fresh
