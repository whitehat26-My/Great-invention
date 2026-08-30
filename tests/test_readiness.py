"""What is real, and what is still a demonstration.

The platform is finished long before the restaurant is in it, and a system
reporting confidently on invented data looks exactly like one reporting on fact.
"""

from __future__ import annotations

import pytest

from restaurant_ai.readiness import PARTIAL, READY, WAITING, look, render, survey

pytestmark = pytest.mark.db


class TestItCoversTheWholeSystem:
    def test_every_agent_is_judged(self, db):
        from restaurant_ai.kernel.registry import all_agents

        picture = look(db)
        assert {a.agent for a in picture.agents} == set(all_agents())

    def test_every_state_is_one_of_three(self, db):
        assert all(a.state in {READY, PARTIAL, WAITING} for a in look(db).agents)

    def test_nothing_blocked_is_left_without_a_way_forward(self, db):
        """ "Insufficient data" is where most of these projects stop."""
        for agent in look(db).agents:
            if agent.state != READY:
                assert agent.fix, f"{agent.person} is blocked with no way forward"


class TestTheGapThatInflatesEveryMargin:
    """A dish with no recipe costs nothing, so it earns 100% and drags the
    average up. Irma will call your worst dishes stars."""

    def test_uncosted_dishes_are_counted(self, db):
        facts = survey(db)
        assert facts["uncosted_dishes"] == facts["dishes"] - facts["costed_dishes"]

    def test_pricing_is_not_ready_while_dishes_are_uncosted(self, db, monkeypatch):
        import restaurant_ai.readiness as r

        facts = survey(db) | {"uncosted_dishes": 40, "dishes": 146, "real_orders": 500}
        monkeypatch.setattr(r, "survey", lambda session: facts)

        irma = next(a for a in look(db).agents if a.agent == "menu_pricing")

        assert irma.state == PARTIAL
        assert "40 of 146" in irma.detail
        assert "100% margin" in irma.fix

    def test_performance_says_which_half_to_trust(self, db, monkeypatch):
        """Revenue is real while prime cost is not, and that is worth saying."""
        import restaurant_ai.readiness as r

        facts = survey(db) | {"uncosted_dishes": 40, "real_orders": 500}
        monkeypatch.setattr(r, "survey", lambda session: facts)

        camelia = next(a for a in look(db).agents if a.agent == "daily_performance")

        assert camelia.state == PARTIAL
        assert "revenue" in camelia.detail.lower()


class TestSalesAreTheHinge:
    def test_without_real_sales_the_money_agents_wait(self, db, monkeypatch):
        import restaurant_ai.readiness as r

        monkeypatch.setattr(r, "survey", lambda session: survey(db) | {"real_orders": 0})
        picture = look(db)

        for name in ("bookkeeping", "daily_performance", "prep_forecaster"):
            agent = next(a for a in picture.agents if a.agent == name)
            assert agent.state == WAITING
            assert "/sold" in agent.fix or "POS" in agent.fix

    def test_the_secretary_needs_nothing_from_the_restaurant(self, db):
        """Her diary is the owner's own, and works from the day they use it."""
        aziera = next(a for a in look(db).agents if a.agent == "reputation")
        assert aziera.state == READY

    def test_pacing_needs_a_live_feed_not_a_recorded_one(self, db, monkeypatch):
        """Hand-recorded sales arrive after service, too late to pace."""
        import restaurant_ai.readiness as r

        monkeypatch.setattr(r, "survey", lambda session: survey(db) | {"real_orders": 9999})
        ciknor = next(a for a in look(db).agents if a.agent == "order_pacing")

        assert ciknor.state == WAITING
        assert "too late to pace" in ciknor.fix


class TestWhatItSaysOutLoud:
    def test_a_demo_only_restaurant_is_told_plainly(self, db, monkeypatch):
        import restaurant_ai.readiness as r

        monkeypatch.setattr(
            r, "survey", lambda session: survey(db) | {"real_orders": 0, "demo_orders": 2514}
        )
        said = render(look(db))

        assert "Nothing below is about your restaurant yet" in said

    def test_a_mixed_restaurant_is_warned_the_totals_add_up_both(self, db, monkeypatch):
        import restaurant_ai.readiness as r

        monkeypatch.setattr(
            r, "survey", lambda session: survey(db) | {"real_orders": 12, "demo_orders": 2514}
        )
        said = render(look(db))

        assert "mixed with" in said

    def test_it_counts_how_many_agents_are_usable(self, db):
        said = render(look(db))
        assert "can tell you the truth today" in said
