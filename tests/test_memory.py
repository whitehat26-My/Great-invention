"""Remembering what was just said.

Before this, every message was answered alone: "how much chicken is left?"
worked and "and rice?" meant nothing. The owner had to restate the whole
question each time, which is how you talk to a search box, not a colleague.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from restaurant_ai import clock, memory

pytestmark = pytest.mark.db

CHAT = "1013758071"


@pytest.fixture(autouse=True)
def _clean(db):
    memory.forget(db, CHAT)
    db.commit()
    yield
    memory.forget(db, CHAT)
    db.commit()


class TestTheThread:
    def test_both_halves_are_kept(self, db):
        """ "Is that a lot?" is about Keanu's last answer, not the restaurant."""
        memory.remember(db, CHAT, memory.OWNER, "how much chicken left?")
        memory.remember(db, CHAT, memory.KEANU, "About 12kg.")
        db.flush()

        said = memory.recent(db, CHAT)

        assert [(t.role, t.text) for t in said] == [
            (memory.OWNER, "how much chicken left?"),
            (memory.KEANU, "About 12kg."),
        ]

    def test_it_reads_oldest_first(self, db):
        """A conversation handed to a model backwards is not a conversation."""
        for i in range(4):
            memory.remember(db, CHAT, memory.OWNER, f"message {i}")
        db.flush()

        assert [t.text for t in memory.recent(db, CHAT)] == [f"message {i}" for i in range(4)]

    def test_only_the_tail_is_kept(self, db):
        """Unbounded history makes the price of "and rice?" grow all day."""
        for i in range(20):
            memory.remember(db, CHAT, memory.OWNER, f"message {i}")
        db.flush()

        said = memory.recent(db, CHAT)

        assert len(said) == 6
        assert said[-1].text == "message 19"

    def test_a_cold_conversation_is_not_resumed(self, db, monkeypatch):
        """Tonight's roster question should not drag in this morning's stock."""
        memory.remember(db, CHAT, memory.OWNER, "this morning's question")
        db.flush()

        later = clock.now() + timedelta(hours=3)
        monkeypatch.setattr(clock, "now", lambda: later)

        assert memory.recent(db, CHAT) == []

    def test_one_chat_cannot_read_another(self, db):
        memory.remember(db, CHAT, memory.OWNER, "ours")
        memory.remember(db, "999888", memory.OWNER, "someone else's")
        db.flush()

        assert [t.text for t in memory.recent(db, CHAT)] == ["ours"]
        memory.forget(db, "999888")

    def test_a_long_paste_cannot_fill_the_prompt(self, db):
        memory.remember(db, CHAT, memory.OWNER, "x" * 5000)
        db.flush()

        assert len(memory.recent(db, CHAT)[0].text) == 600

    def test_nothing_said_is_not_recorded(self, db):
        memory.remember(db, CHAT, memory.OWNER, "   ")
        db.flush()

        assert memory.recent(db, CHAT) == []


class TestForgetting:
    def test_reset_clears_this_chat(self, db):
        memory.remember(db, CHAT, memory.OWNER, "something")
        db.flush()

        assert memory.forget(db, CHAT) == 1
        assert memory.recent(db, CHAT) == []

    def test_old_chat_is_pruned(self, db, monkeypatch):
        """Chat is where a phone number ends up without anyone deciding to keep it."""
        memory.remember(db, CHAT, memory.OWNER, "last week's chat")
        db.flush()

        later = clock.now() + timedelta(days=9)
        monkeypatch.setattr(clock, "now", lambda: later)

        assert memory.prune(db) == 1

    def test_pruning_leaves_this_week_alone(self, db):
        memory.remember(db, CHAT, memory.OWNER, "today's chat")
        db.flush()

        assert memory.prune(db) == 0
        assert len(memory.recent(db, CHAT)) == 1
