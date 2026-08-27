"""Every agent, exercised on the shared kernel.

The contract each of the 13 must satisfy: it runs to completion against the real
database, records an auditable run, and either finishes or parks at an approval
gate with something a human can actually act on.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy import select

from restaurant_ai.db.models import AgentRun, AgentRunStatus
from restaurant_ai.kernel.registry import all_agents, departments, get_agent
from restaurant_ai.kernel.runner import ephemeral_checkpointer, resume_agent, run_agent

pytestmark = pytest.mark.db

AGENT_NAMES = sorted(all_agents())


class TestRegistry:
    def test_thirteen_agents(self):
        assert len(all_agents()) == 13

    def test_six_departments(self):
        assert set(departments()) == {
            "front_of_house",
            "kitchen",
            "supply",
            "marketing",
            "workforce",
            "finance",
        }

    def test_department_composition(self):
        counts = {
            d: len([a for a in all_agents().values() if a.department == d]) for d in departments()
        }
        assert counts == {
            "front_of_house": 3,
            "kitchen": 2,
            "supply": 2,
            "marketing": 2,
            "workforce": 2,
            "finance": 2,
        }

    def test_unknown_agent_raises_with_a_useful_message(self):
        with pytest.raises(KeyError, match="Registered:"):
            get_agent("no_such_agent")

    @pytest.mark.parametrize("name", AGENT_NAMES)
    def test_agent_is_fully_declared(self, name):
        spec = get_agent(name)
        assert spec.title and spec.description
        assert len(spec.system_prompt) > 100, "each agent needs a real operating brief"
        assert spec.perceive is not None
        assert spec.autonomous is not None, "must run without a live model"
        assert spec.model_tier in ("reasoning", "conversational")

    @pytest.mark.parametrize("name", AGENT_NAMES)
    def test_tools_are_documented(self, name):
        for tool in get_agent(name).tools:
            assert tool.description, f"{name}.{tool.name} has no description"
            assert tool.fn is not None


class TestEveryAgentRuns:
    @pytest.mark.parametrize("name", AGENT_NAMES)
    def test_runs_without_error(self, db, name):
        spec = get_agent(name)
        with ephemeral_checkpointer() as cp:
            outcome = run_agent(spec, trigger="test", checkpointer=cp)
        assert outcome.error is None, f"{name} failed: {outcome.error}"

    @pytest.mark.parametrize("name", AGENT_NAMES)
    def test_records_an_auditable_run(self, db, name):
        spec = get_agent(name)
        with ephemeral_checkpointer() as cp:
            outcome = run_agent(spec, trigger="test", checkpointer=cp)
        run = db.get(AgentRun, outcome.run_id)
        assert run is not None
        assert run.agent_name == name
        assert run.department == spec.department
        assert run.thread_id == outcome.thread_id
        assert run.status in (
            AgentRunStatus.COMPLETED,
            AgentRunStatus.AWAITING_APPROVAL,
        )

    @pytest.mark.parametrize("name", AGENT_NAMES)
    def test_produces_a_summary(self, db, name):
        spec = get_agent(name)
        with ephemeral_checkpointer() as cp:
            outcome = run_agent(spec, trigger="test", checkpointer=cp)
        assert outcome.summary, f"{name} produced no summary"


class TestApprovalGates:
    """The four agents that can spend money or publish must stop for a human."""

    GATED = {
        "stock_reorder": "draft_purchase_orders",
        "supplier_invoice": "release_payment",
        "menu_pricing": "propose_changes",
        "reputation": "draft_responses",
    }

    @pytest.mark.parametrize(("agent_name", "tool_name"), sorted(GATED.items()))
    def test_declares_its_gate(self, agent_name, tool_name):
        spec = get_agent(agent_name)
        tool = spec.tool(tool_name)
        assert tool is not None and tool.requires_approval

    @pytest.mark.parametrize(("agent_name", "tool_name"), sorted(GATED.items()))
    def test_does_not_ask_for_approval_of_nothing(self, agent_name, tool_name):
        # Waking someone to approve an empty purchase order or a zero-value
        # payment run is how people learn to rubber-stamp the ones that matter.
        tool = get_agent(agent_name).tool(tool_name)
        assert tool.gate_when is not None, f"{agent_name}.{tool_name} would gate on empty results"
        assert tool.should_gate({}) is False

    def test_stock_reorder_parks_with_an_actionable_request(self, db):
        spec = get_agent("stock_reorder")
        with ephemeral_checkpointer() as cp:
            outcome = run_agent(spec, trigger="test", checkpointer=cp)
        if not outcome.interrupted:
            pytest.skip("Nothing below reorder point in this dataset")

        proposal = outcome.interrupt_payload["proposals"][0]
        assert Decimal(proposal["value"]) > 0
        assert "purchase order" in proposal["summary"].lower()
        # The human must be able to judge it without opening the database.
        assert "on hand" in proposal["detail"]
        assert "reorder point" in proposal["detail"]

    def test_rejecting_a_purchase_order_leaves_nothing_sent(self, db):
        from restaurant_ai.db.models import PurchaseOrder, PurchaseOrderStatus

        spec = get_agent("stock_reorder")
        with ephemeral_checkpointer() as cp:
            outcome = run_agent(spec, trigger="test", checkpointer=cp)
            if not outcome.interrupted:
                pytest.skip("Nothing below reorder point in this dataset")
            resume_agent(
                spec, outcome.thread_id, {"approved": False, "by": "manager"}, checkpointer=cp
            )

        db.expire_all()
        sent = (
            db.execute(
                select(PurchaseOrder).where(
                    PurchaseOrder.created_by_run_id == outcome.run_id,
                    PurchaseOrder.status == PurchaseOrderStatus.SENT,
                )
            )
            .scalars()
            .all()
        )
        assert sent == [], "a rejected order must never reach the supplier"

    def test_approving_a_purchase_order_sends_it(self, db):
        from restaurant_ai.db.models import PurchaseOrder, PurchaseOrderStatus

        spec = get_agent("stock_reorder")
        with ephemeral_checkpointer() as cp:
            outcome = run_agent(spec, trigger="test", checkpointer=cp)
            if not outcome.interrupted:
                pytest.skip("Nothing below reorder point in this dataset")
            resumed = resume_agent(
                spec, outcome.thread_id, {"approved": True, "by": "aishah"}, checkpointer=cp
            )

        assert not resumed.interrupted
        db.expire_all()
        sent = (
            db.execute(
                select(PurchaseOrder).where(
                    PurchaseOrder.created_by_run_id == outcome.run_id,
                    PurchaseOrder.status == PurchaseOrderStatus.SENT,
                )
            )
            .scalars()
            .all()
        )
        assert sent, "approval must actually transmit the order"
        assert all(o.approved_by == "aishah" for o in sent)
