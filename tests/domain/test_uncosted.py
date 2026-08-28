"""An uncosted dish must never read as a free one.

``menu_item_cost`` answers zero for a dish with no bill of materials, because
summing nothing is zero. Left unguarded that is the most expensive kind of
wrong: zero cost is full margin, so the dish nobody has costed outranks every
real one, and sold it contributes nothing to the day's COGS. Both mistakes point
the same way — they make the restaurant look better than it is.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy import select

from restaurant_ai.db.models import MenuItem
from restaurant_ai.domain.costing import costed_menu_items, menu_item_cost

pytestmark = pytest.mark.db


@pytest.fixture
def uncosted_dish(db):
    """A real dish on the menu with no recipe behind it."""
    section_id = db.execute(select(MenuItem.section_id)).scalars().first()
    item = MenuItem(
        sku="MY-TEST-UNCOSTED",
        name="Nasi Goreng Tanpa Resipi",
        section_id=section_id,
        price=Decimal("12.00"),
        prep_seconds=480,
        course=2,
        is_active=True,
    )
    db.add(item)
    db.flush()
    return item


class TestTellingThemApart:
    def test_a_dish_with_no_recipe_costs_zero(self, db, uncosted_dish):
        """The trap itself, stated plainly so the guard has something to guard."""
        assert menu_item_cost(db, uncosted_dish.id) == Decimal("0.00")

    def test_but_it_is_not_reported_as_costed(self, db, uncosted_dish):
        assert uncosted_dish.id not in costed_menu_items(db, [uncosted_dish.id])

    def test_a_dish_with_a_recipe_is(self, db):
        """Found by its recipe, not by being any dish at all.

        Which dishes are active depends on the menu this database was last
        loaded with, and a restaurant that has imported its own menu without
        recipes has none that cost out.
        """
        from restaurant_ai.db.models import Recipe

        costed = [
            r
            for r in db.execute(select(Recipe).where(Recipe.menu_item_id.isnot(None)))
            .scalars()
            .all()
            if r.components
        ]
        if not costed:
            pytest.skip("this database has no costed dish to contrast against")
        assert costed[0].menu_item_id in costed_menu_items(db, [costed[0].menu_item_id])


class TestIrmaLeavesThemOut:
    """Zero cost is full margin: left in, this dish is the best Star on the menu."""

    def test_an_uncosted_dish_is_not_analysed(self, db, uncosted_dish):
        from restaurant_ai import clock
        from restaurant_ai.agents.marketing.menu_pricing import _performances

        analysed = _performances(db, clock.today(), 28)
        assert uncosted_dish.id not in {p.menu_item_id for p in analysed}

    def test_it_cannot_be_crowned_a_star(self, db, uncosted_dish):
        from restaurant_ai import clock
        from restaurant_ai.agents.marketing.menu_pricing import _performances
        from restaurant_ai.domain.pricing import classify_menu

        analysis = classify_menu(_performances(db, clock.today(), 28))
        assert "Tanpa Resipi" not in {c.performance.name for c in analysis.items}

    def test_it_cannot_drag_the_average_margin_up(self, db, uncosted_dish):
        """The quiet damage: a fake 100% dish re-labels honest ones as Plowhorses."""
        from restaurant_ai import clock
        from restaurant_ai.agents.marketing.menu_pricing import _performances
        from restaurant_ai.domain.pricing import classify_menu

        with_dish = classify_menu(_performances(db, clock.today(), 28)).avg_margin
        db.delete(uncosted_dish)
        db.flush()
        without_dish = classify_menu(_performances(db, clock.today(), 28)).avg_margin
        assert with_dish == without_dish

    def test_perceive_says_how_many_it_left_out(self, db, uncosted_dish):
        """Incomplete is acceptable. Silently incomplete is not."""
        from restaurant_ai import clock
        from restaurant_ai.agents.marketing.menu_pricing import perceive
        from restaurant_ai.kernel.spec import ToolContext

        seen = perceive(
            ToolContext(
                session=db,
                run_id="test",
                agent_name="menu_pricing",
                business_date=clock.today(),
                state={},
            )
        )
        # The examples are a capped sample, so the count is what is asserted;
        # the dish itself is checked through the full list.
        from restaurant_ai.agents.marketing.menu_pricing import _uncosted

        assert seen["items_not_costed"] >= 1
        assert seen["items_not_costed"] == len(_uncosted(db))
        assert any("Tanpa Resipi" in name for name in _uncosted(db))


class TestCameliaSaysWhenCogsIsUnderstated:
    """A dish with no recipe explodes to nothing and adds nothing to COGS.

    Sold in volume that understates food cost, prime cost and every verdict
    built on them — silently, and always in the flattering direction.
    """

    @pytest.fixture
    def sold_uncosted(self, db, uncosted_dish):
        from restaurant_ai import clock
        from restaurant_ai.db.models import (
            OrderChannel,
            OrderHeader,
            OrderLine,
            OrderStatus,
        )

        order = OrderHeader(
            order_number="TEST-UNCOSTED-1",
            channel=OrderChannel.DINE_IN,
            status=OrderStatus.CLOSED,
            party_size=2,
            placed_at=clock.now(),
            business_date=clock.today(),
            subtotal=Decimal("24.00"),
            tax=Decimal("0.00"),
            total=Decimal("24.00"),
            discount=Decimal("0.00"),
        )
        db.add(order)
        db.flush()
        db.add(
            OrderLine(
                order_id=order.id,
                menu_item_id=uncosted_dish.id,
                quantity=Decimal("2"),
                unit_price=uncosted_dish.price,
                line_total=Decimal("24.00"),
                course=2,
            )
        )
        db.flush()
        return order

    def _compile(self, db):
        from restaurant_ai import clock
        from restaurant_ai.agents.finance.daily_performance import compile_report
        from restaurant_ai.kernel.spec import ToolContext

        return compile_report(
            ToolContext(
                session=db,
                run_id="test",
                agent_name="daily_performance",
                business_date=clock.today(),
                state={},
            )
        )

    def test_the_understatement_is_declared(self, db, sold_uncosted):
        result = self._compile(db)
        assert result["compiled"]
        assert result["cogs_is_understated"] is True
        assert Decimal(result["units_sold_without_a_recipe"]) == Decimal("2")

    def test_a_fully_costed_day_says_nothing_of_the_sort(self, db, uncosted_dish):
        """The flag must mean something, so it stays off when all is well."""
        db.delete(uncosted_dish)
        db.flush()
        result = self._compile(db)
        if result.get("compiled"):
            assert result["cogs_is_understated"] is False
