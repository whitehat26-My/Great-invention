"""The agent kernel: the graph every one of the 13 agents runs on.

The behaviour that matters most here is the approval gate. A gated action must
prepare its effect, stop, survive being checkpointed, and only take effect once
a human says yes — including when the answer arrives in a different process from
the one that asked.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import select

from restaurant_ai.db.models import AgentAction, AgentRun, AgentRunStatus
from restaurant_ai.kernel.runner import ephemeral_checkpointer, resume_agent, run_agent
from restaurant_ai.kernel.spec import AgentSpec, ToolContext, ToolSpec

pytestmark = pytest.mark.db

# A ledger the fake tools write to, so tests can assert what actually ran.
EFFECTS: list[str] = []


def _reset() -> None:
    EFFECTS.clear()


def _free_tool(context: ToolContext, note: str = "") -> dict[str, Any]:
    EFFECTS.append(f"free:{note}")
    return {"ok": True, "note": note}


def _gated_tool(context: ToolContext, amount: str = "1000.00") -> dict[str, Any]:
    # A gated tool prepares its effect and returns it; it must NOT perform it.
    EFFECTS.append("gated:prepared")
    return {"total": amount, "supplier": "Sing Long", "lines": 3}


def _gated_commit(context: ToolContext, payload: dict[str, Any]) -> dict[str, Any]:
    EFFECTS.append("gated:committed")
    return {"sent": True, "total": payload.get("total")}


def _failing_tool(context: ToolContext) -> dict[str, Any]:
    raise RuntimeError("supplier API is down")


def _make_spec(
    name: str,
    calls: list[dict[str, Any]],
    with_gate: bool = False,
    perceive_result: dict[str, Any] | None = None,
) -> AgentSpec:
    gated = ToolSpec(
        name="draft_po",
        description="Draft a purchase order",
        fn=_gated_tool,
        requires_approval=True,
        approval_value=lambda r: Decimal(str(r.get("total", 0))),
        approval_summary=lambda r: f"Purchase order for {r['supplier']}, {r['total']}",
        approval_detail=lambda r: f"{r['lines']} lines totalling {r['total']}",
    )
    gated.commit_fn = _gated_commit  # type: ignore[attr-defined]

    tools = [
        ToolSpec(name="note", description="Record a note", fn=_free_tool),
        ToolSpec(name="explode", description="Always fails", fn=_failing_tool),
    ]
    if with_gate:
        tools.append(gated)

    return AgentSpec(
        name=name,
        department="test",
        title="Test agent",
        description="Kernel test agent",
        system_prompt="You are a test agent.",
        tools=tools,
        perceive=lambda ctx: perceive_result or {"covers": 42},
        autonomous=lambda ctx, context: {
            "summary": f"Saw {context.get('covers')} covers.",
            "results": {},
            "tool_calls": calls,
        },
    )


@pytest.fixture(autouse=True)
def _clean():
    _reset()
    yield
    _reset()


class TestHappyPath:
    def test_runs_to_completion(self, db):
        spec = _make_spec("t_simple", [{"name": "note", "args": {"note": "hello"}}])
        with ephemeral_checkpointer() as cp:
            outcome = run_agent(spec, checkpointer=cp)
        assert not outcome.interrupted
        assert EFFECTS == ["free:hello"]

    def test_perceive_feeds_reason(self, db):
        spec = _make_spec("t_perceive", [], perceive_result={"covers": 137})
        with ephemeral_checkpointer() as cp:
            outcome = run_agent(spec, checkpointer=cp)
        assert "137" in outcome.summary

    def test_writes_an_audit_run(self, db):
        spec = _make_spec("t_audit", [{"name": "note", "args": {"note": "x"}}])
        with ephemeral_checkpointer() as cp:
            outcome = run_agent(spec, checkpointer=cp)
        run = db.get(AgentRun, outcome.run_id)
        assert run is not None
        assert run.status == AgentRunStatus.COMPLETED
        assert run.agent_name == "t_audit"
        assert run.finished_at is not None

    def test_records_each_tool_call(self, db):
        spec = _make_spec(
            "t_actions",
            [{"name": "note", "args": {"note": "a"}}, {"name": "note", "args": {"note": "b"}}],
        )
        with ephemeral_checkpointer() as cp:
            outcome = run_agent(spec, checkpointer=cp)
        # Explicitly ordered: without ORDER BY the database may return rows in
        # any order, which made this assertion intermittently fail.
        actions = list(
            db.execute(
                select(AgentAction)
                .where(AgentAction.run_id == outcome.run_id)
                .order_by(AgentAction.sequence)
            ).scalars()
        )
        assert len(actions) == 2
        assert [a.sequence for a in actions] == [0, 1]
        assert [a.arguments["note"] for a in actions] == ["a", "b"]

    def test_no_tool_calls_is_fine(self, db):
        spec = _make_spec("t_noop", [])
        with ephemeral_checkpointer() as cp:
            outcome = run_agent(spec, checkpointer=cp)
        assert not outcome.interrupted
        assert EFFECTS == []


class TestApprovalGate:
    def test_gated_action_stops_the_graph(self, db):
        spec = _make_spec("t_gate", [{"name": "draft_po", "args": {}}], with_gate=True)
        with ephemeral_checkpointer() as cp:
            outcome = run_agent(spec, checkpointer=cp)
        assert outcome.interrupted
        assert "gated:prepared" in EFFECTS
        assert "gated:committed" not in EFFECTS, "must not act before approval"

    def test_interrupt_payload_describes_the_action(self, db):
        spec = _make_spec("t_payload", [{"name": "draft_po", "args": {}}], with_gate=True)
        with ephemeral_checkpointer() as cp:
            outcome = run_agent(spec, checkpointer=cp)
        payload = outcome.interrupt_payload
        assert payload["kind"] == "approval_request"
        proposal = payload["proposals"][0]
        assert "Sing Long" in proposal["summary"]
        assert proposal["value"] == "1000.00"

    def test_run_is_marked_awaiting_approval(self, db):
        spec = _make_spec("t_awaiting", [{"name": "draft_po", "args": {}}], with_gate=True)
        with ephemeral_checkpointer() as cp:
            outcome = run_agent(spec, checkpointer=cp)
        db.expire_all()
        run = db.get(AgentRun, outcome.run_id)
        assert run.status == AgentRunStatus.AWAITING_APPROVAL

    def test_approval_commits_the_action(self, db):
        spec = _make_spec("t_approve", [{"name": "draft_po", "args": {}}], with_gate=True)
        with ephemeral_checkpointer() as cp:
            outcome = run_agent(spec, checkpointer=cp)
            assert outcome.interrupted
            resumed = resume_agent(
                spec, outcome.thread_id, {"approved": True, "by": "aishah"}, checkpointer=cp
            )
        assert not resumed.interrupted
        assert EFFECTS == ["gated:prepared", "gated:committed"]

    def test_rejection_does_not_commit(self, db):
        spec = _make_spec("t_reject", [{"name": "draft_po", "args": {}}], with_gate=True)
        with ephemeral_checkpointer() as cp:
            outcome = run_agent(spec, checkpointer=cp)
            resumed = resume_agent(
                spec, outcome.thread_id, {"approved": False, "by": "aishah"}, checkpointer=cp
            )
        assert "gated:committed" not in EFFECTS
        db.expire_all()
        assert db.get(AgentRun, resumed.run_id).status == AgentRunStatus.REJECTED

    def test_rejection_summary_says_so(self, db):
        spec = _make_spec("t_rejsum", [{"name": "draft_po", "args": {}}], with_gate=True)
        with ephemeral_checkpointer() as cp:
            outcome = run_agent(spec, checkpointer=cp)
            resumed = resume_agent(spec, outcome.thread_id, False, checkpointer=cp)
        assert "rejected" in resumed.summary.lower()

    def test_commit_is_audited_separately(self, db):
        spec = _make_spec("t_commitaudit", [{"name": "draft_po", "args": {}}], with_gate=True)
        with ephemeral_checkpointer() as cp:
            outcome = run_agent(spec, checkpointer=cp)
            resume_agent(spec, outcome.thread_id, True, checkpointer=cp)
        db.expire_all()
        actions = list(
            db.execute(select(AgentAction).where(AgentAction.run_id == outcome.run_id)).scalars()
        )
        names = [a.tool_name for a in actions]
        assert "draft_po" in names and "draft_po:commit" in names
        proposal_action = next(a for a in actions if a.tool_name == "draft_po")
        assert proposal_action.is_proposal
        assert proposal_action.committed_at is None

    def test_ungated_and_gated_in_one_run(self, db):
        # The free tool takes effect immediately; the gated one waits.
        spec = _make_spec(
            "t_mixed",
            [{"name": "note", "args": {"note": "first"}}, {"name": "draft_po", "args": {}}],
            with_gate=True,
        )
        with ephemeral_checkpointer() as cp:
            outcome = run_agent(spec, checkpointer=cp)
            assert EFFECTS == ["free:first", "gated:prepared"]
            resume_agent(spec, outcome.thread_id, True, checkpointer=cp)
        assert EFFECTS == ["free:first", "gated:prepared", "gated:committed"]

    def test_ambiguous_decision_is_treated_as_rejection(self, db):
        spec = _make_spec("t_ambig", [{"name": "draft_po", "args": {}}], with_gate=True)
        with ephemeral_checkpointer() as cp:
            outcome = run_agent(spec, checkpointer=cp)
            resume_agent(spec, outcome.thread_id, {"maybe": "later"}, checkpointer=cp)
        assert "gated:committed" not in EFFECTS


class TestResilience:
    def test_a_failing_tool_does_not_kill_the_run(self, db):
        spec = _make_spec(
            "t_partial",
            [{"name": "explode", "args": {}}, {"name": "note", "args": {"note": "after"}}],
        )
        with ephemeral_checkpointer() as cp:
            outcome = run_agent(spec, checkpointer=cp)
        assert not outcome.interrupted
        assert EFFECTS == ["free:after"], "later tools must still run"

    def test_tool_failure_is_recorded(self, db):
        spec = _make_spec("t_failrec", [{"name": "explode", "args": {}}])
        with ephemeral_checkpointer() as cp:
            outcome = run_agent(spec, checkpointer=cp)
        action = db.execute(
            select(AgentAction).where(AgentAction.run_id == outcome.run_id)
        ).scalar_one()
        assert "supplier API is down" in action.error

    def test_unknown_tool_is_recorded_not_raised(self, db):
        spec = _make_spec("t_unknown", [{"name": "nonexistent", "args": {}}])
        with ephemeral_checkpointer() as cp:
            outcome = run_agent(spec, checkpointer=cp)
        action = db.execute(
            select(AgentAction).where(AgentAction.run_id == outcome.run_id)
        ).scalar_one()
        assert "Unknown tool" in action.error


class TestThreading:
    def test_each_run_gets_its_own_thread(self, db):
        spec = _make_spec("t_threads", [])
        with ephemeral_checkpointer() as cp:
            a = run_agent(spec, checkpointer=cp)
            b = run_agent(spec, checkpointer=cp)
        assert a.thread_id != b.thread_id
        assert a.run_id != b.run_id

    def test_thread_id_is_persisted_for_later_resume(self, db):
        spec = _make_spec("t_persist", [{"name": "draft_po", "args": {}}], with_gate=True)
        with ephemeral_checkpointer() as cp:
            outcome = run_agent(spec, checkpointer=cp)
        run = db.get(AgentRun, outcome.run_id)
        assert run.thread_id == outcome.thread_id


class TestIdleRuns:
    """Nothing to do is an outcome, and it should cost nothing to reach.

    The pacing agent is scheduled every five minutes through service — right for
    a ticket that lands at 19:03, wrong as a description of how often there is
    one. Before this, every empty wake-up still called the model to be told the
    kitchen was empty: 156 calls a day, more than every other agent combined.
    """

    def test_idle_agent_never_reaches_the_planner(self, db):
        planned: list[str] = []

        spec = _make_spec("t_idle", [{"name": "note", "args": {"note": "hi"}}])
        spec.perceive = lambda ctx: {"open_order_lines": 0}
        spec.idle_when = lambda context: (
            None if context.get("open_order_lines") else "Nothing waiting."
        )
        spec.autonomous = lambda ctx, context: (  # type: ignore[assignment]
            planned.append("planned"),
            {"summary": "planned", "results": {}, "tool_calls": []},
        )[1]

        outcome = run_agent(spec, trigger="schedule")

        assert planned == [], "an idle run must not reach the planner at all"
        assert outcome.summary == "Nothing waiting."

    def test_idle_run_is_still_a_completed_run(self, db):
        """It has to be audited: a run that leaves no trace looks like beat died."""
        spec = _make_spec("t_idle_audit", [])
        spec.perceive = lambda ctx: {}
        spec.idle_when = lambda context: "Nothing waiting."

        outcome = run_agent(spec, trigger="schedule")

        run = db.get(AgentRun, outcome.run_id)
        assert run is not None
        assert run.status == AgentRunStatus.COMPLETED
        assert run.summary == "Nothing waiting."
        assert run.finished_at is not None
        actions = list(
            db.execute(select(AgentAction).where(AgentAction.run_id == outcome.run_id)).scalars()
        )
        assert actions == []

    def test_work_present_runs_normally(self, db):
        spec = _make_spec("t_busy", [{"name": "note", "args": {"note": "hi"}}])
        spec.perceive = lambda ctx: {"covers": 42, "open_order_lines": 3}
        spec.idle_when = lambda context: (
            None if context.get("open_order_lines") else "Nothing waiting."
        )

        outcome = run_agent(spec, trigger="schedule")

        assert outcome.summary == "Saw 42 covers."

    def test_a_broken_predicate_does_the_work_anyway(self, db):
        """Skipping a run the restaurant needed is worse than paying for one it did not."""
        spec = _make_spec("t_idle_broken", [{"name": "note", "args": {"note": "hi"}}])

        def explode(context: dict[str, Any]) -> str | None:
            raise RuntimeError("predicate is wrong")

        spec.idle_when = explode

        outcome = run_agent(spec, trigger="schedule")

        assert outcome.summary == "Saw 42 covers."

    def test_agents_that_fetch_their_own_work_are_not_gated(self):
        """An empty database is exactly what the review sweep exists to change.

        Reputation's `perceive` counts reviews already stored; its sweep goes out
        to the platforms for new ones. Gating it on the stored count would mean
        it never ingested a review again — the failure would be silent, and it
        would look like nobody had reviewed the restaurant.
        """
        from restaurant_ai.kernel.registry import get_agent

        assert get_agent("reputation").idle_when is None
        assert get_agent("order_pacing").idle_when is not None
