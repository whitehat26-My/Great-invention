"""BOM explosion and costing, against the real seeded database.

These run on PostgreSQL with the demo restaurant loaded, because the behaviour
under test is fundamentally about recursive relational data: a plated dish
consuming sub-recipes that themselves consume raw ingredients.
"""

from datetime import timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select

from restaurant_ai.db.models import Ingredient, MenuItem, Recipe, RecipeComponent
from restaurant_ai.domain.costing import (
    BomError,
    cost_breakdown,
    cost_of_requirement,
    explode_many,
    explode_menu_item,
    explode_recipe,
    ingredients_carrying,
    menu_item_allergens,
    menu_item_cost,
)

D = Decimal
pytestmark = pytest.mark.db


def _item(db, sku: str) -> MenuItem:
    return db.execute(select(MenuItem).where(MenuItem.sku == sku)).scalar_one()


class TestExplosion:
    def test_resolves_through_sub_recipes(self, db):
        # Nasi Lemak uses coconut rice, rendang paste and sambal, none of which
        # are raw ingredients. The result must contain only raw ingredients.
        requirement = explode_menu_item(db, _item(db, "MNU-NASILEMK").id)
        assert requirement
        ids = set(requirement)
        raw = {i.id for i in db.execute(select(Ingredient).where(Ingredient.id.in_(ids))).scalars()}
        assert ids == raw, "explosion must bottom out at raw ingredients"

    def test_pulls_in_ingredients_only_reachable_via_a_sub_recipe(self, db):
        # Jasmine rice appears in Nasi Lemak only through the coconut rice
        # sub-recipe, never as a direct component.
        item = _item(db, "MNU-NASILEMK")
        requirement = explode_menu_item(db, item.id)
        rice = db.execute(select(Ingredient).where(Ingredient.code == "ING-RICE-JAS")).scalar_one()
        assert rice.id in requirement

        recipe = db.execute(select(Recipe).where(Recipe.menu_item_id == item.id)).scalar_one()
        direct = {c.ingredient_id for c in recipe.components if c.ingredient_id}
        assert rice.id not in direct

    def test_scales_linearly_with_quantity(self, db):
        item = _item(db, "MNU-CHARKWAY")
        one = explode_menu_item(db, item.id, 1)
        three = explode_menu_item(db, item.id, 3)
        for ingredient_id, amount in one.items():
            assert three[ingredient_id] == amount * 3

    def test_sub_recipe_scales_by_batch_fraction(self, db):
        # Taking 70 g from a recipe that yields 1200 g pulls in 70/1200 of it.
        paste = db.execute(select(Recipe).where(Recipe.code == "SUB-REND-PST")).scalar_one()
        full = explode_recipe(db, paste.id, D("1"))
        half = explode_recipe(db, paste.id, D("0.5"))
        for ingredient_id, amount in full.items():
            assert half[ingredient_id] == amount / 2

    def test_zero_quantity_yields_nothing(self, db):
        assert explode_menu_item(db, _item(db, "MNU-KOPIO").id, 0) == {}

    def test_item_without_a_recipe_returns_empty(self, db):
        assert explode_menu_item(db, "no-such-item") == {}

    def test_missing_recipe_raises(self, db):
        with pytest.raises(BomError, match="not found"):
            explode_recipe(db, "no-such-recipe")

    def test_cycle_is_detected(self, db):
        # A sub-recipe that (indirectly) contains itself must fail loudly rather
        # than recursing until the stack gives out.
        a = Recipe(code="CYC-A", name="Cycle A", yield_qty=D("100"), yield_uom="g")
        b = Recipe(code="CYC-B", name="Cycle B", yield_qty=D("100"), yield_uom="g")
        db.add_all([a, b])
        db.flush()
        db.add_all(
            [
                RecipeComponent(recipe_id=a.id, sub_recipe_id=b.id, quantity=D("10"), uom="g"),
                RecipeComponent(recipe_id=b.id, sub_recipe_id=a.id, quantity=D("10"), uom="g"),
            ]
        )
        db.flush()
        with pytest.raises(BomError, match="cycle"):
            explode_recipe(db, a.id)

    def test_explode_many_aggregates(self, db):
        nasi = _item(db, "MNU-NASILEMK")
        rendang = _item(db, "MNU-BEEFREND")
        combined = explode_many(db, {nasi.id: D("2"), rendang.id: D("3")})
        separate_nasi = explode_menu_item(db, nasi.id, 2)
        separate_rendang = explode_menu_item(db, rendang.id, 3)
        for ingredient_id in set(separate_nasi) | set(separate_rendang):
            expected = separate_nasi.get(ingredient_id, D("0")) + separate_rendang.get(
                ingredient_id, D("0")
            )
            assert combined[ingredient_id] == expected

    def test_explode_many_skips_non_positive(self, db):
        assert explode_many(db, {_item(db, "MNU-KOPIO").id: D("0")}) == {}


class TestCosting:
    def test_plate_cost_is_positive_and_below_price(self, db):
        for sku in ("MNU-NASILEMK", "MNU-CHARKWAY", "MNU-TEHTARIK"):
            item = _item(db, sku)
            cost = menu_item_cost(db, item.id)
            assert D("0") < cost < item.price

    def test_breakdown_lines_sum_to_the_total(self, db):
        breakdown = cost_breakdown(db, _item(db, "MNU-NASILEMK").id)
        assert (
            sum(line.cost for line in breakdown.lines).quantize(D("0.01")) == breakdown.total_cost
        )

    def test_breakdown_is_sorted_by_cost(self, db):
        breakdown = cost_breakdown(db, _item(db, "MNU-NASILEMK").id)
        costs = [line.cost for line in breakdown.lines]
        assert costs == sorted(costs, reverse=True)

    def test_margin_percentages_are_consistent(self, db):
        breakdown = cost_breakdown(db, _item(db, "MNU-BEEFREND").id)
        assert breakdown.gross_margin == breakdown.price - breakdown.total_cost
        assert float(breakdown.gross_margin_pct + breakdown.food_cost_pct) == pytest.approx(
            1.0, abs=0.001
        )

    def test_food_cost_is_plausible(self, db):
        # A menu where everything costs 5% or 90% of its price is a broken BOM.
        for item in db.execute(select(MenuItem).where(MenuItem.is_active)).scalars():
            breakdown = cost_breakdown(db, item.id)
            assert D("0.05") < breakdown.food_cost_pct < D("0.60"), f"{item.sku} looks wrong"

    def test_missing_item_raises(self, db):
        with pytest.raises(BomError, match="not found"):
            cost_breakdown(db, "no-such-item")

    def test_cost_of_requirement(self, db):
        item = _item(db, "MNU-CHARKWAY")
        requirement = explode_menu_item(db, item.id, 4)
        assert float(cost_of_requirement(db, requirement)) == pytest.approx(
            float(menu_item_cost(db, item.id) * 4), abs=0.05
        )

    def test_cost_of_empty_requirement(self, db):
        assert cost_of_requirement(db, {}) == D("0")


class TestAllergens:
    def test_derived_through_a_sub_recipe(self, db):
        # Belacan (shrimp paste) is inside sambal tumis, which is inside Nasi
        # Lemak. Shellfish must surface even though no direct component has it.
        allergens = menu_item_allergens(db, _item(db, "MNU-NASILEMK").id)
        assert "shellfish" in allergens

    def test_direct_allergens_are_found(self, db):
        allergens = menu_item_allergens(db, _item(db, "MNU-NASILEMK").id)
        assert {"peanut", "egg", "fish"} <= allergens

    def test_a_clean_dish_has_none_of_the_flagged_allergens(self, db):
        allergens = menu_item_allergens(db, _item(db, "MNU-CENDOL").id)
        assert "peanut" not in allergens and "shellfish" not in allergens

    def test_carriers_name_the_offending_ingredient(self, db):
        carriers = ingredients_carrying(db, _item(db, "MNU-NASILEMK").id, {"peanut", "shellfish"})
        assert "Belacan (shrimp paste)" in carriers["shellfish"]
        assert "Peanuts, roasted" in carriers["peanut"]

    def test_carriers_ignore_unrequested_allergens(self, db):
        carriers = ingredients_carrying(db, _item(db, "MNU-NASILEMK").id, {"peanut"})
        assert set(carriers) == {"peanut"}

    def test_carriers_for_a_safe_dish(self, db):
        assert ingredients_carrying(db, _item(db, "MNU-KOPIO").id, {"shellfish"}) == {}

    def test_item_without_recipe(self, db):
        assert menu_item_allergens(db, "no-such-item") == set()


class TestUnitsPerCover:
    """The units-to-covers conversion the roster is sized on."""

    def test_is_a_plausible_basket_size(self, db):
        from restaurant_ai import clock
        from restaurant_ai.agents.common import units_per_cover

        basket = units_per_cover(db, days=56, until=clock.today())
        # Under one would mean guests sharing a single dish between them; over
        # four would mean everyone ordering a banquet. Either points at the
        # join-inflation bug this replaced.
        assert D("1.0") < basket < D("4.0"), f"{basket} dishes per guest is not plausible"

    def test_counts_each_order_s_guests_once(self, db):
        # Summing party_size across joined order lines counts a table once per
        # dish, which inverts the ratio.
        from sqlalchemy import func, select

        from restaurant_ai import clock
        from restaurant_ai.agents.common import units_per_cover
        from restaurant_ai.db.models import OrderHeader, OrderLine

        until = clock.today()
        since = until - timedelta(days=56)
        units = db.execute(
            select(func.sum(OrderLine.quantity))
            .select_from(OrderLine)
            .join(OrderHeader, OrderLine.order_id == OrderHeader.id)
            .where(OrderHeader.business_date >= since, OrderHeader.business_date < until)
        ).scalar_one()
        covers = db.execute(
            select(func.sum(OrderHeader.party_size)).where(
                OrderHeader.business_date >= since, OrderHeader.business_date < until
            )
        ).scalar_one()

        expected = (D(str(units)) / D(str(covers))).quantize(D("0.0001"))
        assert units_per_cover(db, days=56, until=until) == expected

    def test_no_trading_history_returns_one(self, db):
        from datetime import date as _date

        from restaurant_ai.agents.common import units_per_cover

        assert units_per_cover(db, days=1, until=_date(1990, 1, 1)) == D("1")
