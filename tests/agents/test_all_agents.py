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
    def test_eleven_agents(self):
        # Was thirteen. Reservations and conversational ordering were retired:
        # both conversed with guests live under their own names, and the order
        # agent gave allergen advice.
        assert len(all_agents()) == 11

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
            "front_of_house": 1,
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
    """Every agent that can spend money or publish must stop for a human."""

    # A list, not a dict: one agent can have more than one gated tool, and
    # Franky has two.
    GATED = [
        ("stock_reorder", "draft_purchase_orders"),
        ("supplier_invoice", "release_payment"),
        ("menu_pricing", "propose_changes"),
        ("reputation", "draft_responses"),
        ("social_content", "schedule_content"),
        ("social_content", "build_reengagement"),
    ]

    def test_nothing_reaches_the_public_unsupervised(self):
        """The architecture's whole claim, checked against every tool.

        Franky scheduled posts straight to the platforms with no gate at all,
        while Aziera's review replies and Irma's price moves both stopped for
        someone. It was harmless only because SOCIAL_PROVIDER defaults to fake
        — set it live and he posted on his own.
        """
        outward = {
            ("social_content", "schedule_content"),
            ("social_content", "build_reengagement"),
            ("reputation", "draft_responses"),
        }
        for agent_name, tool_name in outward:
            tool = get_agent(agent_name).tool(tool_name)
            assert tool is not None, f"{agent_name}.{tool_name} is gone"
            assert tool.requires_approval, (
                f"{agent_name}.{tool_name} reaches guests or the public unsupervised"
            )

    @pytest.mark.parametrize(("agent_name", "tool_name"), GATED)
    def test_declares_its_gate(self, agent_name, tool_name):
        spec = get_agent(agent_name)
        tool = spec.tool(tool_name)
        assert tool is not None and tool.requires_approval

    @pytest.mark.parametrize(("agent_name", "tool_name"), GATED)
    def test_does_not_ask_for_approval_of_nothing(self, agent_name, tool_name):
        # Waking someone to approve an empty purchase order or a zero-value
        # payment run is how people learn to rubber-stamp the ones that matter.
        tool = get_agent(agent_name).tool(tool_name)
        assert tool.gate_when is not None, f"{agent_name}.{tool_name} would gate on empty results"
        assert tool.should_gate({}) is False

    def test_a_gate_covers_everything_its_tool_can_change(self):
        """`gate_when` decides what escapes review, so it has to be exhaustive.

        menu_pricing gated on `price_changes` alone. On the first live run the
        model proposed no price moves and three bundles — each a change to what
        the restaurant charges — and every one of them went through unapproved
        while the run reported "completed".
        """
        gate = get_agent("menu_pricing").tool("propose_changes").gate_when
        assert gate is not None
        assert gate({"price_changes": 0, "bundles": 0}) is False
        assert gate({"price_changes": 2, "bundles": 0}) is True
        assert gate({"price_changes": 0, "bundles": 3}) is True, (
            "bundles change what the restaurant charges and must face a human"
        )

    def test_stock_reorder_parks_with_an_actionable_request(self, db, stock_is_low):
        spec = get_agent("stock_reorder")
        with ephemeral_checkpointer() as cp:
            outcome = run_agent(spec, trigger="test", checkpointer=cp)
        assert outcome.interrupted, (
            "stock is below its reorder point, so the agent must propose a purchase order"
        )

        proposal = outcome.interrupt_payload["proposals"][0]
        assert Decimal(proposal["value"]) > 0
        assert "purchase order" in proposal["summary"].lower()
        # The human must be able to judge it without opening the database.
        assert "on hand" in proposal["detail"]
        assert "reorder point" in proposal["detail"]

    def test_rejecting_a_purchase_order_leaves_nothing_sent(self, db, stock_is_low):
        from restaurant_ai.db.models import PurchaseOrder, PurchaseOrderStatus

        spec = get_agent("stock_reorder")
        with ephemeral_checkpointer() as cp:
            outcome = run_agent(spec, trigger="test", checkpointer=cp)
            assert outcome.interrupted, "a short ingredient must produce a proposal"
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

    def test_approving_a_purchase_order_sends_it(self, db, stock_is_low):
        from restaurant_ai.db.models import PurchaseOrder, PurchaseOrderStatus

        spec = get_agent("stock_reorder")
        with ephemeral_checkpointer() as cp:
            outcome = run_agent(spec, trigger="test", checkpointer=cp)
            assert outcome.interrupted, "a short ingredient must produce a proposal"
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


class TestNames:
    """Every agent is a person to whoever has to act on what it asks for."""

    def test_all_thirteen_have_one(self):
        missing = [n for n, s in all_agents().items() if not s.person]
        assert missing == [], f"unnamed: {', '.join(sorted(missing))}"

    def test_the_names_are_distinct(self):
        people = [s.person for s in all_agents().values()]
        assert len(set(people)) == len(people), "two agents answering to one name"

    def test_the_slug_is_still_the_key(self):
        """`name` is what the CLI, the schedule and every audit row key off.

        Naming an agent must not move it — a renamed slug orphans its whole run
        history, which is the record of what it has already been allowed to do.
        """
        for slug, spec in all_agents().items():
            assert spec.name == slug
            assert spec.name.islower() and " " not in spec.name

    def test_an_approval_says_who_is_asking(self):
        from restaurant_ai.kernel.registry import display_name

        label = display_name("stock_reorder")
        assert label.startswith("Rain")
        assert "Stock Tracking" in label

    def test_an_unknown_agent_still_renders(self):
        # A stale approval row from a retired agent must not raise inside a
        # Slack card. Degrade to the slug.
        from restaurant_ai.kernel.registry import display_name

        assert display_name("retired_agent") == "retired_agent"

    def test_the_agent_is_told_its_own_name(self):
        from restaurant_ai.kernel.graph import _system_prompt

        for spec in all_agents().values():
            assert f"Your name is {spec.person}." in _system_prompt(spec)


class TestFrankyPublishesOnlyWhenTold:
    """Drafting a post and publishing it are separate steps with a human between.

    `schedule_content` used to call the platform inside the tool, so the post
    was out before anyone saw it. A drafted post now has no `external_ref`;
    that is what tells it apart from a published one.
    """

    def test_drafting_does_not_publish(self, db):
        from restaurant_ai.db.models import SocialPost

        spec = get_agent("social_content")
        with ephemeral_checkpointer() as cp:
            outcome = run_agent(spec, trigger="test", checkpointer=cp)

        assert outcome.interrupted, "posts must face a human before they go out"
        drafted = (
            db.execute(select(SocialPost).where(SocialPost.run_id == outcome.run_id))
            .scalars()
            .all()
        )
        assert drafted, "the posts should have been written"
        assert all(p.external_ref is None for p in drafted), (
            "a post reached the platform before anyone approved it"
        )

    def test_approving_publishes_them(self, db):
        from restaurant_ai.db.models import SocialPost

        spec = get_agent("social_content")
        with ephemeral_checkpointer() as cp:
            outcome = run_agent(spec, trigger="test", checkpointer=cp)
            assert outcome.interrupted
            resumed = resume_agent(
                spec, outcome.thread_id, {"approved": True, "by": "sharif"}, checkpointer=cp
            )

        assert not resumed.interrupted
        db.expire_all()
        published = (
            db.execute(select(SocialPost).where(SocialPost.run_id == outcome.run_id))
            .scalars()
            .all()
        )
        assert published, "the posts should still exist"
        assert all(p.external_ref for p in published), "approval must actually publish them"

    def test_the_card_carries_the_copy_a_human_has_to_judge(self):
        tool = get_agent("social_content").tool("schedule_content")
        result = {
            "scheduled": 1,
            "posts": [
                {
                    "post_id": "p1",
                    "platform": "instagram",
                    "scheduled_for": "2026-08-28T10:01:00",
                    "body": "Tiger prawns, charred over flame.",
                    "why": "High margin, low awareness.",
                }
            ],
        }
        assert "1 post(s)" in tool.summarise(result)
        detail = tool.detail_of(result)
        assert "Tiger prawns" in detail and "instagram" in detail

    def test_an_offer_is_drafted_to_nobody(self, db):
        """`issued_count` at zero is the draft marker: it exists, it reached no one."""
        from restaurant_ai.db.models import PromoOffer

        spec = get_agent("social_content")
        with ephemeral_checkpointer() as cp:
            outcome = run_agent(spec, trigger="test", checkpointer=cp)
        offers = (
            db.execute(select(PromoOffer).where(PromoOffer.run_id == outcome.run_id))
            .scalars()
            .all()
        )
        assert all(o.issued_count == 0 for o in offers)

    def test_an_empty_run_asks_nobody(self):
        for tool_name in ("schedule_content", "build_reengagement"):
            tool = get_agent("social_content").tool(tool_name)
            assert tool.should_gate({}) is False


class TestCurrencyReadsRight:
    def test_a_post_uses_the_symbol_not_the_iso_code(self, db):
        """MYR belongs in a journal; RM belongs on a menu.

        The symbol was hardcoded, so changing CURRENCY would have left posts
        quoting ringgit in whatever the restaurant had moved to.
        """
        from restaurant_ai.config import get_settings
        from restaurant_ai.db.models import SocialPost

        spec = get_agent("social_content")
        with ephemeral_checkpointer() as cp:
            outcome = run_agent(spec, trigger="test", checkpointer=cp)
        posts = (
            db.execute(select(SocialPost).where(SocialPost.run_id == outcome.run_id))
            .scalars()
            .all()
        )
        assert posts
        symbol = get_settings().currency_symbol
        assert any(symbol in p.body for p in posts)

    def test_the_symbol_and_the_code_are_both_configured(self):
        from restaurant_ai.config import get_settings

        settings = get_settings()
        assert settings.currency and settings.currency_symbol
        assert settings.currency != settings.currency_symbol
