"""The live-model path, driven by a scripted model instead of the network.

``test_llm_wiring`` stops at the network boundary: it proves a model can be
built and tools can be bound. Everything past that point — whether a tool the
model chose actually runs, whether the result comes back to it, whether a
two-step plan is even possible — went unexercised, which is how the path came to
contain a condition that meant it never ran at all.

The model here is a stub that returns whatever the test tells it to and records
what it was shown. No key, no network, runs in CI. What it cannot check is
whether Claude makes *good* choices; it checks that the choices it makes are
carried out, and that what comes back is true.
"""

from __future__ import annotations

import json
from decimal import Decimal
from typing import Any

import pytest
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import Field

from restaurant_ai.db.models import AgentRun, AgentRunStatus
from restaurant_ai.kernel import llm
from restaurant_ai.kernel.runner import ephemeral_checkpointer, run_agent
from restaurant_ai.kernel.spec import AgentSpec, ToolContext, ToolSpec

pytestmark = pytest.mark.db

EFFECTS: list[str] = []


# --------------------------------------------------------------------------
# A model that says what the test tells it to, and remembers what it was asked
# --------------------------------------------------------------------------


class ScriptedModel(BaseChatModel):
    """Stands in for Claude.

    ``reply`` is handed the full message list and the turn number and returns
    the AIMessage to answer with, so a test can make the second turn depend on
    what the first turn's tools actually returned — which is the whole point of
    the loop and cannot be checked with a fixed list of canned replies.
    """

    reply: Any
    seen: list[list[BaseMessage]] = Field(default_factory=list)
    bound: list[Any] = Field(default_factory=list)

    @property
    def _llm_type(self) -> str:
        return "scripted"

    def bind_tools(self, tools: Any, **kwargs: Any) -> ScriptedModel:
        # Returns self rather than a RunnableBinding so the test can still read
        # `seen` and `bound` off the object the graph is holding.
        self.bound = list(tools)
        return self

    def _generate(self, messages: list[BaseMessage], **kwargs: Any) -> ChatResult:
        self.seen.append(list(messages))
        message = self.reply(messages, len(self.seen))
        return ChatResult(generations=[ChatGeneration(message=message)])

    @property
    def turns(self) -> int:
        return len(self.seen)

    def tool_replies(self, turn: int) -> list[ToolMessage]:
        """The tool results the model was shown at the start of the given turn."""
        return [m for m in self.seen[turn - 1] if isinstance(m, ToolMessage)]

    @property
    def prompt(self) -> str:
        return str(self.seen[0][1].content)


def call(name: str, args: dict[str, Any] | None = None, *, id: str = "tu_1") -> AIMessage:
    return AIMessage(content="", tool_calls=[{"name": name, "args": args or {}, "id": id}])


def says(text: str) -> AIMessage:
    return AIMessage(content=text)


@pytest.fixture
def model(monkeypatch: pytest.MonkeyPatch):
    """Put the graph on the live path, with the stub standing in for Claude."""

    def install(reply: Any) -> ScriptedModel:
        stub = ScriptedModel(reply=reply)
        monkeypatch.setattr(llm, "get_model", lambda tier="conversational": stub)
        monkeypatch.setattr(llm, "is_fake", lambda: False)
        return stub

    return install


# --------------------------------------------------------------------------
# Tools
# --------------------------------------------------------------------------


def _note(context: ToolContext, note: str = "") -> dict[str, Any]:
    EFFECTS.append(f"note:{note}")
    return {"ok": True, "note": note}


def _find_table(context: ToolContext, party_size: int = 2) -> dict[str, Any]:
    # The id is generated here, so a booking that quotes it can only have got it
    # by reading this result.
    return {"table_id": f"T{party_size * 7}", "seats": party_size}


def _book(context: ToolContext, table_id: str = "") -> dict[str, Any]:
    EFFECTS.append(f"booked:{table_id}")
    return {"booked": table_id}


def _explode(context: ToolContext) -> dict[str, Any]:
    raise RuntimeError("supplier API is down")


def _draft_po(context: ToolContext) -> dict[str, Any]:
    EFFECTS.append("po:prepared")
    return {"total": "1400.00", "supplier": "Sing Long", "lines": 3}


def _commit_po(context: ToolContext, payload: dict[str, Any]) -> dict[str, Any]:
    EFFECTS.append("po:sent")
    return {"sent": True}


_GATED = ToolSpec(
    name="draft_po",
    description="Draft a purchase order",
    fn=_draft_po,
    requires_approval=True,
    approval_value=lambda r: Decimal(str(r["total"])),
    approval_summary=lambda r: f"Purchase order for {r['supplier']}, {r['total']}",
    approval_detail=lambda r: f"{r['lines']} lines",
)
_GATED.commit_fn = _commit_po  # type: ignore[attr-defined]


def spec(name: str, *, max_iterations: int = 6, perceived: dict[str, Any] | None = None):
    return AgentSpec(
        name=name,
        department="test",
        title="Live test agent",
        description="Drives the live-model path.",
        system_prompt="You are a test agent. " + "Behave sensibly. " * 20,
        tools=[
            ToolSpec(name="note", description="Record a note", fn=_note),
            ToolSpec(name="find_table", description="Find a table", fn=_find_table),
            ToolSpec(name="book", description="Book a table", fn=_book),
            ToolSpec(name="explode", description="Always fails", fn=_explode),
            _GATED,
        ],
        perceive=lambda ctx: perceived or {"covers": 42},
        autonomous=lambda ctx, c: {
            "summary": "deterministic path ran",
            "results": {},
            "tool_calls": [],
        },
        max_iterations=max_iterations,
    )


@pytest.fixture(autouse=True)
def _clean():
    EFFECTS.clear()
    yield
    EFFECTS.clear()


def run(agent: AgentSpec, **kwargs: Any):
    with ephemeral_checkpointer() as cp:
        return run_agent(agent, checkpointer=cp, **kwargs)


# --------------------------------------------------------------------------


class TestPathSelection:
    """Which planner runs. The bug here made the whole live path unreachable."""

    def test_a_configured_model_is_actually_asked(self, db, model):
        stub = model(lambda messages, turn: says("Nothing needs doing."))
        outcome = run(spec("live_selected"))
        assert stub.turns == 1, "the model was never consulted"
        assert outcome.summary == "Nothing needs doing."

    def test_an_agent_with_a_deterministic_path_still_uses_the_model(self, db, model):
        # The regression: every agent defines an autonomous path, so preferring
        # it whenever one existed meant a real API key changed nothing at all.
        stub = model(lambda messages, turn: says("I decided this myself."))
        outcome = run(spec("live_not_shadowed"))
        assert stub.turns == 1
        assert "deterministic path ran" not in outcome.summary

    def test_the_fake_provider_takes_the_deterministic_path(self, db):
        # No stub installed and no key: nothing may reach for a model.
        outcome = run(spec("fake_selected"))
        assert outcome.summary.startswith("deterministic path ran")

    def test_force_path_pins_a_single_run(self, db, model):
        stub = model(lambda messages, turn: says("model spoke"))
        outcome = run(spec("forced_det"), trigger_payload={"_force_path": "deterministic"})
        assert stub.turns == 0
        assert "deterministic path ran" in outcome.summary


class TestToolExecution:
    def test_a_tool_the_model_chooses_is_run_with_its_arguments(self, db, model):
        model(
            lambda messages, turn: (
                call("note", {"note": "prep the fryer"}) if turn == 1 else says("Noted.")
            )
        )
        outcome = run(spec("live_tool"))
        assert EFFECTS == ["note:prep the fryer"]
        assert outcome.summary == "Noted."

    def test_the_model_is_shown_what_the_tool_returned(self, db, model):
        stub = model(
            lambda messages, turn: call("note", {"note": "hello"}) if turn == 1 else says("Done.")
        )
        run(spec("live_feedback"))
        replies = stub.tool_replies(2)
        assert len(replies) == 1
        assert json.loads(str(replies[0].content))["note"] == "hello"

    def test_a_two_step_plan_carries_the_first_result_into_the_second(self, db, model):
        """The thing a one-shot planner cannot do.

        Turn two reads the table id out of turn one's tool result, so this
        passes only if the loop genuinely delivered it.
        """

        def reply(messages: list[BaseMessage], turn: int) -> AIMessage:
            if turn == 1:
                return call("find_table", {"party_size": 4})
            if turn == 2:
                found = json.loads(str(messages[-1].content))
                return call("book", {"table_id": found["table_id"]}, id="tu_2")
            return says("Booked.")

        model(reply)
        outcome = run(spec("live_two_step"))
        assert EFFECTS == ["booked:T28"]
        assert outcome.summary == "Booked."

    def test_an_unknown_tool_is_reported_rather_than_silently_dropped(self, db, model):
        stub = model(lambda messages, turn: call("teleport") if turn == 1 else says("Ah."))
        run(spec("live_unknown"))
        assert "no tool named" in str(stub.tool_replies(2)[0].content)

    def test_every_tool_call_gets_a_reply(self, db, model):
        """A tool_use with no matching tool_result is a 400 on the next request.

        Unknown tool, failing tool and gated tool all have to answer, and each
        answers on a different branch of `act`.
        """

        def reply(messages: list[BaseMessage], turn: int) -> AIMessage:
            if turn == 1:
                return AIMessage(
                    content="",
                    tool_calls=[
                        {"name": "note", "args": {"note": "a"}, "id": "tu_a"},
                        {"name": "explode", "args": {}, "id": "tu_b"},
                        {"name": "teleport", "args": {}, "id": "tu_c"},
                    ],
                )
            return says("Fine.")

        stub = model(reply)
        run(spec("live_all_answered"))
        answered = {m.tool_call_id for m in stub.tool_replies(2)}
        assert answered == {"tu_a", "tu_b", "tu_c"}


class TestTheLoop:
    def test_it_stops_at_the_iteration_limit(self, db, model):
        stub = model(lambda messages, turn: call("note", {"note": str(turn)}))
        outcome = run(spec("live_capped", max_iterations=3))
        assert stub.turns == 3
        assert EFFECTS == ["note:1", "note:2", "note:3"]
        # And says so, rather than presenting an unfinished job as a finished one.
        assert "reasoning limit" in outcome.summary

    def test_a_turn_that_calls_nothing_ends_the_run(self, db, model):
        stub = model(lambda messages, turn: says("Nothing to do."))
        run(spec("live_short", max_iterations=6))
        assert stub.turns == 1

    def test_the_plan_is_not_replayed_on_the_next_turn(self, db, model):
        # `_planned_calls` survives in context; if `act` does not clear it the
        # same call runs again on every pass round the loop.
        model(lambda messages, turn: call("note", {"note": "once"}) if turn == 1 else says("Done."))
        run(spec("live_no_replay"))
        assert EFFECTS == ["note:once"]

    def test_the_transcript_accumulates(self, db, model):
        stub = model(
            lambda messages, turn: (
                call("note", {"note": str(turn)}, id=f"tu_{turn}") if turn < 3 else says("Done.")
            )
        )
        run(spec("live_transcript", max_iterations=6))
        # Turn 3 can still see turn 1: system, situation, then both exchanges.
        assert len(stub.seen[2]) == len(stub.seen[0]) + 4
        assert {m.tool_call_id for m in stub.tool_replies(3)} == {"tu_1", "tu_2"}


class TestTheApprovalGate:
    def test_a_gated_tool_parks_the_run(self, db, model):
        model(lambda messages, turn: call("draft_po"))
        outcome = run(spec("live_gate"))
        assert outcome.interrupted
        assert EFFECTS == ["po:prepared"], "the gated action must not have happened"

    def test_the_model_cannot_iterate_past_the_gate(self, db, model):
        # It asks for a note on every turn it is given. It must not be given one.
        stub = model(
            lambda messages, turn: (
                call("draft_po") if turn == 1 else call("note", {"note": "carrying on regardless"})
            )
        )
        run(spec("live_gate_stops_loop", max_iterations=6))
        assert stub.turns == 1
        assert "note:carrying on regardless" not in EFFECTS

    def test_a_parked_run_still_leaves_a_well_formed_transcript(self, db, model):
        """Every tool_use answered, even on the turn that parked the run.

        Nothing asks the model anything after a park, so this is not about what
        it reads next — it is about the transcript that gets checkpointed. An
        unanswered tool_use in there is a 400 for whoever resumes from it.
        """

        def reply(messages: list[BaseMessage], turn: int) -> AIMessage:
            return AIMessage(
                content="",
                tool_calls=[
                    {"name": "draft_po", "args": {}, "id": "tu_g"},
                    {"name": "note", "args": {"note": "x"}, "id": "tu_n"},
                ],
            )

        model(reply)
        outcome = run(spec("live_gate_transcript"))
        assert outcome.interrupted

        replies = [m for m in outcome.state["messages"] if isinstance(m, ToolMessage)]
        assert {m.tool_call_id for m in replies} == {"tu_g", "tu_n"}
        # And the gated one reads as prepared, not as done.
        gated = next(m for m in replies if m.tool_call_id == "tu_g")
        assert "awaiting human approval" in str(gated.content)
        assert EFFECTS == ["po:prepared", "note:x"]


class TestWhatTheModelIsShown:
    def test_the_trigger_payload_reaches_the_prompt(self, db, model):
        """What actually caused the run.

        Without this the order agent is answering a guest whose question it has
        never been shown — it only ever saw the standing picture of the
        restaurant that `perceive` loads.
        """
        stub = model(lambda messages, turn: says("ok"))
        run(
            spec("live_payload"),
            trigger_payload={"guest_message": "anything without peanuts?"},
        )
        assert "anything without peanuts?" in stub.prompt

    def test_internal_keys_stay_out_of_the_prompt(self, db, model):
        stub = model(lambda messages, turn: says("ok"))
        run(spec("live_internal"), trigger_payload={"_force_path": "model"})
        assert "_force_path" not in stub.prompt

    def test_perceive_reaches_the_prompt(self, db, model):
        stub = model(lambda messages, turn: says("ok"))
        run(spec("live_perceive", perceived={"below_reorder_point": 11}))
        assert "below_reorder_point" in stub.prompt

    def test_the_agents_tools_are_bound(self, db, model):
        stub = model(lambda messages, turn: says("ok"))
        run(spec("live_bound"))
        assert {t.name for t in stub.bound} == {
            "note",
            "find_table",
            "book",
            "explode",
            "draft_po",
        }


class TestSummaries:
    def test_thinking_never_reaches_the_summary(self, db, model):
        """Opus 5 thinks by default, so `content` is a list of blocks.

        A plain `str()` over it puts the chain of thought into the run summary,
        the audit trail, and whatever a human reads in Slack.
        """
        model(
            lambda messages, turn: AIMessage(
                content=[
                    {
                        "type": "thinking",
                        "thinking": "The guest is probably lying about the allergy.",
                        "signature": "sig",
                    },
                    {"type": "text", "text": "All clear — no peanuts in that dish."},
                ]
            )
        )
        outcome = run(spec("live_thinking"))
        assert outcome.summary == "All clear — no peanuts in that dish."
        assert "lying" not in outcome.summary

    def test_a_silent_tool_turn_does_not_blank_the_summary(self, db, model):
        # Turn 2 calls a tool and says nothing; the sentence from turn 1 is the
        # only explanation of the run there is, and must survive.
        def reply(messages: list[BaseMessage], turn: int) -> AIMessage:
            if turn == 1:
                return AIMessage(
                    content="Checking the fryer.",
                    tool_calls=[{"name": "note", "args": {"note": "a"}, "id": "tu_1"}],
                )
            return AIMessage(
                content="", tool_calls=[{"name": "note", "args": {"note": "b"}, "id": "tu_2"}]
            )

        model(reply)
        outcome = run(spec("live_silent", max_iterations=2))
        assert "Checking the fryer." in outcome.summary

    def test_the_last_word_wins(self, db, model):
        model(
            lambda messages, turn: (
                AIMessage(
                    content="Checking.",
                    tool_calls=[{"name": "note", "args": {"note": "a"}, "id": "tu_1"}],
                )
                if turn == 1
                else says("Fryer is fine.")
            )
        )
        outcome = run(spec("live_last_word"))
        assert outcome.summary == "Fryer is fine."


class TestThinkingSurvivesTheLoop:
    def test_a_thinking_block_is_still_there_on_the_next_turn(self, db, model):
        """Otherwise the second request is a 400.

        With thinking on, Anthropic requires the thinking block — signature and
        all — to come back with the assistant turn that made the tool call. If
        anything between here and the next request drops it, every multi-turn
        live run fails on turn two, and finding that out costs money.
        """

        def reply(messages: list[BaseMessage], turn: int) -> AIMessage:
            if turn == 1:
                return AIMessage(
                    content=[
                        {
                            "type": "thinking",
                            "thinking": "Four covers, so the smallest table that fits.",
                            "signature": "sig-abc",
                        },
                        {"type": "text", "text": "Finding a table."},
                    ],
                    tool_calls=[{"name": "find_table", "args": {"party_size": 4}, "id": "tu_1"}],
                )
            return says("Found one.")

        stub = model(reply)
        outcome = run(spec("live_thinking_roundtrip"))

        assert stub.turns == 2
        replayed = next(m for m in stub.seen[1] if isinstance(m, AIMessage))
        blocks = [b for b in replayed.content if isinstance(b, dict)]
        thinking = next(b for b in blocks if b.get("type") == "thinking")
        assert thinking["signature"] == "sig-abc"
        # Kept for the model, kept out of what a human reads.
        assert "smallest table" not in outcome.summary


class TestFailures:
    def test_a_failing_tool_is_reported_back_to_the_model(self, db, model):
        stub = model(lambda messages, turn: call("explode") if turn == 1 else says("Noted."))
        run(spec("live_tool_error"))
        assert "supplier API is down" in str(stub.tool_replies(2)[0].content)

    def test_the_model_can_recover_from_a_failing_tool(self, db, model):
        def reply(messages: list[BaseMessage], turn: int) -> AIMessage:
            if turn == 1:
                return call("explode")
            if turn == 2 and "Error" in str(messages[-1].content):
                return call("note", {"note": "fell back"}, id="tu_2")
            return says("Recovered.")

        model(reply)
        outcome = run(spec("live_recovery"))
        assert EFFECTS == ["note:fell back"]
        assert outcome.summary.startswith("Recovered.")
        # Still worth telling an operator a tool went down — but not worth
        # telling them the work was lost, because it was not.
        assert "explode" in outcome.summary
        assert "was not done" not in outcome.summary

    def test_a_run_whose_every_tool_failed_is_not_reported_as_success(self, db, model):
        # The regression that let the pacing agent crash unnoticed for a
        # fortnight: the run said COMPLETED while the tool raised every time.
        model(lambda messages, turn: call("explode") if turn == 1 else says("Oh well."))
        outcome = run(spec("live_all_failed"))
        run_row = db.get(AgentRun, outcome.run_id)
        assert run_row is not None
        assert run_row.status == AgentRunStatus.FAILED
        assert "explode" in run_row.summary


class TestWhatTheAuditTrailRecords:
    def test_the_model_that_actually_answered_is_recorded(self, db, model, monkeypatch):
        """`agent_run.model` is the only record of which model decided something.

        It was read off the Anthropic settings whatever the provider, so every
        Gemini run was filed as `claude-opus-5`.
        """
        from restaurant_ai.config import get_settings, reset_settings_cache

        monkeypatch.setenv("LLM_PROVIDER", "google")
        monkeypatch.setenv("GOOGLE_API_KEY", "k")
        reset_settings_cache()
        model(lambda messages, turn: says("done"))

        outcome = run(spec("audit_model"))
        run_row = db.get(AgentRun, outcome.run_id)
        assert run_row is not None
        assert run_row.model == get_settings().google_model_conversational
        assert not run_row.model.startswith("claude")
        reset_settings_cache()

    def test_the_fake_provider_is_recorded_as_fake(self, db):
        outcome = run(spec("audit_fake"))
        run_row = db.get(AgentRun, outcome.run_id)
        assert run_row is not None and run_row.model == "fake"


class TestWhereTheRestaurantIs:
    """Settings the platform has always had and never told a model about."""

    def test_the_operating_context_precedes_the_agents_brief(self):
        from restaurant_ai.config import get_settings
        from restaurant_ai.kernel.graph import _system_prompt
        from restaurant_ai.kernel.registry import get_agent

        settings = get_settings()
        agent = get_agent("ordering")
        prompt = _system_prompt(agent)

        assert settings.restaurant_name in prompt
        assert settings.currency in prompt
        assert settings.timezone in prompt
        # The agent's own brief still follows it, intact.
        assert prompt.endswith(agent.system_prompt)

    def test_the_model_is_told_which_currency(self, db, model):
        """It quoted a guest "$49.80" for a dish priced in ringgit.

        Nothing in the prompt said otherwise, and a bare number defaults to
        dollars.
        """
        stub = model(lambda messages, turn: says("ok"))
        run(spec("currency"))
        system = str(stub.seen[0][0].content)
        assert "MYR" in system
        assert "currency symbol from somewhere else" in system
