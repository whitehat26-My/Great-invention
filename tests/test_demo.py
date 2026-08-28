"""Telling the demo restaurant from the real one.

`seed` invents a fortnight of trading with plausible covers, revenue and prime
cost. Camelia closes the day on it and the brief arrives on a phone reporting
the takings of a restaurant that does not exist. The danger is not the day the
owner types `seed` and remembers — it is the week the real restaurant opens and
real orders start landing beside the invented ones.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest

from restaurant_ai import clock, demo
from restaurant_ai.db.models import OrderChannel, OrderHeader, OrderStatus

pytestmark = pytest.mark.db


def _order(db, number: str, *, day=None):
    """A distinct order. Numbers are suffixed because a seeded database
    already owns every H<real-date>-<n> this test would otherwise invent."""
    import uuid

    number = f"{number}-{uuid.uuid4().hex[:6]}"
    order = OrderHeader(
        order_number=number,
        channel=OrderChannel.DINE_IN,
        status=OrderStatus.CLOSED,
        party_size=2,
        placed_at=clock.now(),
        business_date=day or clock.today(),
        subtotal=Decimal("30.00"),
        tax=Decimal("0.00"),
        total=Decimal("30.00"),
        discount=Decimal("0.00"),
    )
    db.add(order)
    db.flush()
    return order


class TestCountingWhatWasInvented:
    def test_seeded_orders_are_recognised_by_their_number(self, db):
        """`seed` numbers its orders H<yymmdd>-<n>, and nothing else does."""
        before = demo.synthetic_orders(db)
        _order(db, "H260828-00001")
        assert demo.synthetic_orders(db) == before + 1

    def test_a_real_order_is_not_counted_as_demo(self, db):
        before_demo = demo.synthetic_orders(db)
        before_real = demo.real_orders(db)
        _order(db, "POS-4471")

        assert demo.synthetic_orders(db) == before_demo
        assert demo.real_orders(db) == before_real + 1

    def test_a_day_can_be_asked_about_on_its_own(self, db):
        tomorrow = clock.today() + timedelta(days=1)
        _order(db, "H260828-09999", day=tomorrow)
        assert demo.synthetic_orders(db, tomorrow) == 1
        assert demo.real_orders(db, tomorrow) == 0


class TestSayingSo:
    def test_a_purely_seeded_database_says_none_of_it_is_real(self, db):
        _order(db, "H260828-00002")
        said = demo.describe(db)
        assert said is not None
        assert "demo data" in said
        assert "not real trading" in said

    def test_a_mixed_database_says_the_totals_mix_both(self, db):
        """The week the restaurant opens: the dangerous one."""
        _order(db, "H260828-00003")
        _order(db, "POS-0001")
        said = demo.describe(db)
        assert said is not None
        assert "mix both" in said

    def test_a_real_restaurant_is_told_nothing(self, db, quiet_orders):
        """Once it is real this must go silent, not become a banner to skip."""
        _order(db, "POS-0002")
        assert demo.describe(db) is None


@pytest.fixture
def quiet_orders(db):
    """No orders at all, so this test's world is only what it creates."""
    for row in db.query(OrderHeader).all():
        db.delete(row)
    db.flush()
    return db


class TestTheBriefCarriesIt:
    def test_the_money_section_ends_with_the_caveat(self, db):
        from restaurant_ai.brief import build_brief

        _order(db, "H260828-00004")
        money = build_brief(db).sections["money"]
        assert any("demo data" in line for line in money)

    def test_a_real_restaurant_gets_no_caveat(self, db, quiet_orders):
        from restaurant_ai.brief import build_brief

        _order(db, "POS-0003")
        money = build_brief(db).sections["money"]
        assert not any("demo data" in line for line in money)


class TestDoctorStatesIt:
    def test_it_is_reported_as_a_fact_not_a_fault(self, db):
        """Demo data is a state of the restaurant, not something broken."""
        from restaurant_ai.doctor import diagnose, render

        _order(db, "H260828-00005")
        report = diagnose()
        trading = next(c for c in report.checks if c.name == "trading data")

        assert trading.ok, "demo data must not read as a failure"
        assert "demo order" in trading.detail
        # But the advice still reaches the page.
        assert "reset-db" in render(report)
