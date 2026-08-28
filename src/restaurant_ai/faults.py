"""Turning an exception into a sentence the owner can act on.

A real screenshot from the approvals chat: a phone-sized bubble containing a
psycopg ``UndefinedTable``, the full SELECT with all seventeen columns, the
``LINE 2:`` caret, and a link to the SQLAlchemy error docs — where a sentence
should have been. The owner learned nothing except that something is broken,
which they could already see.

Two jobs. **Condense**: keep the first line, drop the SQL echo and the
documentation footnote, cap the length. **Recognise**: the handful of failures
that actually happen have names and fixes, and "the database has no tables yet"
is a different sentence from "the database is not running" even though psycopg
raises for both.
"""

from __future__ import annotations

# A phone bubble. Long enough for a real message, short enough that nobody has
# to scroll a paragraph of stack trace to learn one fact.
_MAX_FAULT_CHARS = 180


def short_fault(exc: Exception) -> str:
    """One line: what went wrong, in words, without the SQL.

    SQLAlchemy appends the statement, the parameters and a docs link to every
    message. That is exactly right in a log and exactly wrong on a phone.
    """
    known = recognise(exc)
    if known is not None:
        return known

    text = str(exc)
    # Everything SQLAlchemy bolts on after the driver's own complaint.
    for marker in ("[SQL:", "[parameters:", "(Background on this error"):
        cut = text.find(marker)
        if cut != -1:
            text = text[:cut]
    text = " ".join(text.split())
    if len(text) > _MAX_FAULT_CHARS:
        text = text[:_MAX_FAULT_CHARS].rstrip() + "…"
    return f"{type(exc).__name__}: {text}" if text else type(exc).__name__


def recognise(exc: Exception) -> str | None:
    """The failures that actually happen, named with their fix.

    None when this is not one of them — then ``short_fault`` falls back to
    condensing, rather than guessing at a fix it does not know.
    """
    text = str(exc).lower()

    if "does not exist" in text and "relation" in text:
        return (
            "the database has no tables yet — `restaurant-ai up` creates them "
            "(or `alembic upgrade head` on its own)"
        )
    if "current transaction is aborted" in text:
        # A follow-on from the first failure in the same transaction, not a
        # fault of its own. Saying so stops five sections reporting five bugs.
        return "skipped — an earlier query in this transaction failed"
    if "connection refused" in text or "connection timeout" in text:
        return "the database is not running — `restaurant-ai up` starts it"
    if "password authentication failed" in text:
        return "the database refused the password in .env (POSTGRES_PASSWORD)"
    return None


def database_has_no_tables(exc: Exception) -> bool:
    """Whether this is the empty-schema case, which migrations fix."""
    text = str(exc).lower()
    return "does not exist" in text and "relation" in text
