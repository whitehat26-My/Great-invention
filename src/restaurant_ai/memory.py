"""What was said a moment ago.

The question desk could answer anything and remember nothing. Every message
arrived alone, so "how much chicken is left?" worked perfectly and "and rice?"
meant nothing at all — the owner had to restate the whole question every time,
which is how you talk to a search box rather than to a person.

Three decisions shape it:

- **A window, not a transcript.** The last few exchanges, and only within the
  last hour. A conversation resumed after dinner is a new conversation, and
  dragging this morning's stock question into tonight's roster question helps
  nobody. It also bounds what the prompt costs: without a limit the price of
  answering "and rice?" grows all day.
- **In the database, not in the process.** The listener gets restarted — by the
  supervisor when a child dies, by a reboot, by an upgrade. Memory that survives
  the question but not the restart is worse than none, because the owner cannot
  predict which they are getting.
- **Both halves are kept.** What Keanu said matters as much as what was asked:
  "is that a lot?" is a question about his last answer, not about the
  restaurant.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from restaurant_ai import clock
from restaurant_ai.db.models import ConversationTurn
from restaurant_ai.logging_setup import get_logger

log = get_logger(__name__)

OWNER = "owner"
KEANU = "keanu"

# Six turns is three exchanges — enough for "and rice?" and the follow-up to
# that, and short of the point where an old topic starts steering a new one.
_WINDOW_TURNS = 6

# After an hour, the next message is a new conversation rather than a
# continuation of one nobody remembers starting.
_WINDOW = timedelta(hours=1)

# A pasted menu or a long complaint should not be able to fill the prompt on
# its own, and the sense of a turn survives being cut.
_MAX_TURN_CHARS = 600


@dataclass(frozen=True)
class Turn:
    role: str
    text: str


def remember(session: Session, chat_id: str | int, role: str, text: str) -> None:
    """Record one thing that was said. Never raises into the conversation."""
    text = (text or "").strip()
    if not text:
        return
    session.add(
        ConversationTurn(
            chat_id=str(chat_id),
            role=role,
            text=text[:_MAX_TURN_CHARS],
            said_at=clock.now(),
        )
    )


def recent(session: Session, chat_id: str | int, limit: int = _WINDOW_TURNS) -> list[Turn]:
    """The tail of this conversation, oldest first, or nothing if it went cold."""
    rows = list(
        session.execute(
            select(ConversationTurn)
            .where(
                ConversationTurn.chat_id == str(chat_id),
                ConversationTurn.said_at >= clock.now() - _WINDOW,
            )
            .order_by(ConversationTurn.said_at.desc())
            .limit(limit)
        ).scalars()
    )
    return [Turn(role=row.role, text=row.text) for row in reversed(rows)]


def forget(session: Session, chat_id: str | int) -> int:
    """Drop this chat's history — what /reset does, and what a new topic wants."""
    result = session.execute(
        delete(ConversationTurn).where(ConversationTurn.chat_id == str(chat_id))
    )
    return int(getattr(result, "rowcount", 0) or 0)


def prune(session: Session, older_than: timedelta = timedelta(days=7)) -> int:
    """Housekeeping. Nothing here is worth keeping for a week.

    Chat is the one thing the owner types freely into this system, so it is
    where a phone number or a supplier's price ends up without anyone deciding
    that it should be stored. Keeping it forever is a liability that earns
    nothing: the answers are already in the tables the agents write.
    """
    result = session.execute(
        delete(ConversationTurn).where(ConversationTurn.said_at < clock.now() - older_than)
    )
    dropped = int(getattr(result, "rowcount", 0) or 0)
    if dropped:
        log.info("pruned conversation history", turns=dropped)
    return dropped
