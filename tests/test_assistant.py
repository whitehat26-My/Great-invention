"""The owner's question desk.

The property that matters most is that asking cannot become acting. The desk is
read-only by construction — no tools are bound to the model — and these tests
hold that line where a prompt-level promise would not.
"""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

from restaurant_ai.assistant import answer, build_snapshot

pytestmark = pytest.mark.db


class TestTheSnapshot:
    def test_it_carries_what_the_agents_themselves_see(self, db):
        snapshot = build_snapshot(db)
        assert set(snapshot) >= {
            "business_date",
            "today",
            "needs_your_approval",
            "what_each_agent_sees",
        }
        # Reported by person, the way the owner refers to them.
        seen = snapshot["what_each_agent_sees"]
        assert "Rain" in seen
        assert "Stock Tracking" in seen["Rain"]

    def test_it_reports_the_restaurants_real_numbers(self, db, stock_is_low):
        snapshot = build_snapshot(db)
        assert "below_reorder_point" in snapshot["what_each_agent_sees"]["Rain"]

    def test_a_broken_view_is_a_line_not_a_failure(self, db, monkeypatch):
        """An owner asking about the roster still gets it when stock is broken."""
        from restaurant_ai import brief as brief_module

        real = brief_module._perceive

        def selective(session, agent_name, business_date):
            if agent_name == "stock_reorder":
                raise RuntimeError("stock view is on fire")
            return real(session, agent_name, business_date)

        monkeypatch.setattr(brief_module, "_perceive", selective)
        snapshot = build_snapshot(db)

        assert "unavailable" in snapshot["what_each_agent_sees"]["Rain"]
        assert "stock view is on fire" in snapshot["what_each_agent_sees"]["Rain"]
        # Henry is untouched.
        assert "unavailable" not in snapshot["what_each_agent_sees"]["Henry"]

    def test_a_sweeping_view_is_capped_before_it_reaches_a_prompt(self, db, monkeypatch):
        """A one-line question must not cost a whole table of context."""
        from restaurant_ai import brief as brief_module

        monkeypatch.setattr(brief_module, "_perceive", lambda *a, **k: {"items": ["x" * 100] * 500})
        snapshot = build_snapshot(db)
        for view in snapshot["what_each_agent_sees"].values():
            assert len(view) < 3000
            assert "truncated" in view


class TestItCannotAct:
    def test_the_model_is_given_no_tools_at_all(self, db, monkeypatch):
        """Read-only by construction: nothing is bound, so nothing can be called.

        A prompt that says "do not change anything" is a request. This asserts
        the stronger property — the model has no way to act even if it tries.
        """
        captured = {}

        class Recorder:
            def bind_tools(self, *args, **kwargs):  # pragma: no cover - must not happen
                captured["bound"] = args
                raise AssertionError("the question desk must never bind tools")

            def invoke(self, messages):
                captured["messages"] = messages

                class Response:
                    content = "Rain does the ordering, and it needs your approval."

                return Response()

        monkeypatch.setattr("restaurant_ai.kernel.llm.is_fake", lambda: False)
        monkeypatch.setattr("restaurant_ai.kernel.llm.get_model", lambda tier: Recorder())

        reply = answer("Order 50kg of prawns right now.", session=db)

        assert "bound" not in captured
        assert "Rain" in reply

    def test_the_brief_says_it_cannot_change_anything(self, db, monkeypatch):
        captured = {}

        class Recorder:
            def invoke(self, messages):
                captured["system"] = messages[0].content

                class Response:
                    content = "ok"

                return Response()

        monkeypatch.setattr("restaurant_ai.kernel.llm.is_fake", lambda: False)
        monkeypatch.setattr("restaurant_ai.kernel.llm.get_model", lambda tier: Recorder())
        answer("anything", session=db)

        system = captured["system"]
        assert "cannot change anything" in system
        assert "no tools" in system
        assert "Never imply that" in system

    def test_it_is_told_the_currency_and_timezone(self, db, monkeypatch):
        """The order agent once quoted a ringgit dish in dollars. Not again."""
        captured = {}

        class Recorder:
            def invoke(self, messages):
                captured["system"] = messages[0].content

                class Response:
                    content = "ok"

                return Response()

        monkeypatch.setattr("restaurant_ai.kernel.llm.is_fake", lambda: False)
        monkeypatch.setattr("restaurant_ai.kernel.llm.get_model", lambda tier: Recorder())
        answer("what is the average check?", session=db)

        assert "RM" in captured["system"]
        assert "Asia/Kuala_Lumpur" in captured["system"]


class TestAnswering:
    def test_an_empty_question_invites_one(self, db):
        assert "Ask me anything" in answer("   ", session=db)

    def test_without_a_model_it_still_reports_real_numbers(self, db):
        """ "No model configured" must not mean "no answer"."""
        reply = answer("how are we doing?", session=db)
        assert "No language model is configured" in reply
        assert "money:" in reply

    def test_a_long_answer_is_cut_to_fit_a_phone(self, db, monkeypatch):
        class Recorder:
            def invoke(self, messages):
                class Response:
                    content = "x" * 5000

                return Response()

        monkeypatch.setattr("restaurant_ai.kernel.llm.is_fake", lambda: False)
        monkeypatch.setattr("restaurant_ai.kernel.llm.get_model", lambda tier: Recorder())

        reply = answer("tell me everything", session=db)
        assert len(reply) <= 1201
        assert reply.endswith("…")

    def test_thinking_never_reaches_the_owner(self, db, monkeypatch):
        """Claude thinks in blocks; a raw str() would send the owner its reasoning."""

        class Recorder:
            def invoke(self, messages):
                class Response:
                    content = [
                        {"type": "thinking", "thinking": "the owner must not read this"},
                        {"type": "text", "text": "Seven ingredients are low."},
                    ]

                return Response()

        monkeypatch.setattr("restaurant_ai.kernel.llm.is_fake", lambda: False)
        monkeypatch.setattr("restaurant_ai.kernel.llm.get_model", lambda tier: Recorder())

        reply = answer("what is low?", session=db)
        assert reply == "Seven ingredients are low."
        assert "must not read this" not in reply


class TestTheCli:
    def test_ask_answers_from_the_command_line(self, db):
        from restaurant_ai.cli import app

        result = CliRunner().invoke(app, ["ask", "how are we doing?"])
        assert result.exit_code == 0, result.output
        assert "money:" in result.output
