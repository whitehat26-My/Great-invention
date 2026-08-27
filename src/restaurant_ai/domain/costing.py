"""BOM explosion, plate costing and allergen derivation.

This module is the hinge the rest of the platform turns on. Selling one Nasi
Lemak has to become "deduct 320 g of coconut rice base, which is itself 150 g of
jasmine rice and 90 ml of coconut milk, plus 180 g of chicken thigh...", and the
same walk gives you the plate cost and the allergen set. Doing it once, here,
means the stock agent, the forecaster, the pricing agent and the order agent all
agree on what a dish actually contains.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from restaurant_ai.db.models import Ingredient, MenuItem, Recipe, RecipeComponent
from restaurant_ai.domain.units import convert

ZERO = Decimal("0")


class BomError(ValueError):
    """Raised for a malformed bill of materials (cycles, missing recipes)."""


@dataclass
class CostBreakdown:
    """A plate's cost, decomposed to ingredient level."""

    menu_item_id: str
    sku: str
    name: str
    price: Decimal
    total_cost: Decimal
    lines: list[IngredientCost] = field(default_factory=list)

    @property
    def gross_margin(self) -> Decimal:
        return self.price - self.total_cost

    @property
    def gross_margin_pct(self) -> Decimal:
        if self.price <= 0:
            return ZERO
        return (self.gross_margin / self.price).quantize(Decimal("0.0001"))

    @property
    def food_cost_pct(self) -> Decimal:
        if self.price <= 0:
            return ZERO
        return (self.total_cost / self.price).quantize(Decimal("0.0001"))


@dataclass
class IngredientCost:
    ingredient_id: str
    code: str
    name: str
    quantity: Decimal
    uom: str
    unit_cost: Decimal
    cost: Decimal


def explode_recipe(
    session: Session,
    recipe_id: str,
    multiplier: Decimal = Decimal("1"),
    _visiting: frozenset[str] = frozenset(),
) -> dict[str, Decimal]:
    """Resolve a recipe to raw ingredient quantities in each ingredient's base unit.

    ``multiplier`` scales the whole recipe. Sub-recipes are scaled by the
    fraction of a batch consumed: needing 70 g from a recipe that yields 1200 g
    pulls in 70/1200 of its components.
    """
    if recipe_id in _visiting:
        raise BomError(f"Recipe cycle detected at {recipe_id}")

    recipe = session.execute(
        select(Recipe)
        .where(Recipe.id == recipe_id)
        .options(selectinload(Recipe.components).selectinload(RecipeComponent.ingredient))
    ).scalar_one_or_none()
    if recipe is None:
        raise BomError(f"Recipe {recipe_id} not found")

    totals: dict[str, Decimal] = {}
    for component in recipe.components:
        if component.ingredient_id is not None:
            ingredient = component.ingredient
            if ingredient is None:  # pragma: no cover - FK guarantees this
                raise BomError(f"Component references missing ingredient {component.ingredient_id}")
            amount = convert(
                component.quantity,
                component.uom,
                ingredient.base_uom,
                session=session,
                ingredient_id=ingredient.id,
            )
            amount *= multiplier
            totals[ingredient.id] = totals.get(ingredient.id, ZERO) + amount
            continue

        # A sub-recipe: work out what fraction of one batch is being consumed.
        sub = session.get(Recipe, component.sub_recipe_id)
        if sub is None:
            raise BomError(f"Sub-recipe {component.sub_recipe_id} not found")
        needed = convert(component.quantity, component.uom, sub.yield_uom, session=session)
        if sub.yield_qty <= 0:
            raise BomError(f"Sub-recipe {sub.code} has a non-positive yield")
        batch_fraction = needed / sub.yield_qty
        nested = explode_recipe(
            session,
            sub.id,
            multiplier=multiplier * batch_fraction,
            _visiting=_visiting | {recipe_id},
        )
        for ing_id, amount in nested.items():
            totals[ing_id] = totals.get(ing_id, ZERO) + amount

    # Drop zero rows. A zero-quantity requirement is not a fact about the
    # recipe, and stock_movement carries a `quantity <> 0` CHECK that such a
    # row would violate the moment the stock agent tried to write it.
    return {ing_id: amount for ing_id, amount in totals.items() if amount > 0}


def explode_menu_item(
    session: Session, menu_item_id: str, quantity: Decimal | int = 1
) -> dict[str, Decimal]:
    """Ingredient requirement, in base units, for selling ``quantity`` of an item."""
    recipe = session.execute(
        select(Recipe).where(Recipe.menu_item_id == menu_item_id)
    ).scalar_one_or_none()
    if recipe is None:
        return {}
    return explode_recipe(session, recipe.id, multiplier=Decimal(str(quantity)))


def explode_order_line(
    session: Session, menu_item_id: str, quantity: Decimal
) -> dict[str, Decimal]:
    """Alias used by the stock agent when deducting a POS sale."""
    return explode_menu_item(session, menu_item_id, quantity)


def menu_item_cost(session: Session, menu_item_id: str) -> Decimal:
    """Plate cost: the summed cost of one portion's raw ingredients."""
    requirement = explode_menu_item(session, menu_item_id, 1)
    if not requirement:
        return ZERO
    ingredients = _load_ingredients(session, list(requirement))
    total = sum(
        (amount * ingredients[ing_id].cost_per_base_unit for ing_id, amount in requirement.items()),
        ZERO,
    )
    return Decimal(total).quantize(Decimal("0.01"))


def cost_breakdown(session: Session, menu_item_id: str) -> CostBreakdown:
    """Plate cost with the per-ingredient detail an agent can explain to a human."""
    item = session.get(MenuItem, menu_item_id)
    if item is None:
        raise BomError(f"Menu item {menu_item_id} not found")

    requirement = explode_menu_item(session, menu_item_id, 1)
    ingredients = _load_ingredients(session, list(requirement))

    lines: list[IngredientCost] = []
    total = ZERO
    for ing_id, amount in sorted(
        requirement.items(),
        key=lambda kv: kv[1] * ingredients[kv[0]].cost_per_base_unit,
        reverse=True,
    ):
        ingredient = ingredients[ing_id]
        cost = (amount * ingredient.cost_per_base_unit).quantize(Decimal("0.0001"))
        total += cost
        lines.append(
            IngredientCost(
                ingredient_id=ing_id,
                code=ingredient.code,
                name=ingredient.name,
                quantity=amount.quantize(Decimal("0.0001")),
                uom=ingredient.base_uom,
                unit_cost=ingredient.cost_per_base_unit,
                cost=cost,
            )
        )

    return CostBreakdown(
        menu_item_id=menu_item_id,
        sku=item.sku,
        name=item.name,
        price=item.price,
        total_cost=total.quantize(Decimal("0.01")),
        lines=lines,
    )


def menu_item_allergens(session: Session, menu_item_id: str) -> set[str]:
    """Every allergen reachable through the item's full BOM.

    Derived from the recipe rather than a hand-maintained label, so a change to
    a shared sub-recipe (belacan in the sambal, say) propagates to every dish
    that uses it without anyone having to remember.
    """
    requirement = explode_menu_item(session, menu_item_id, 1)
    if not requirement:
        return set()
    ingredients = _load_ingredients(session, list(requirement))
    found: set[str] = set()
    for ing_id in requirement:
        found.update(ingredients[ing_id].allergens)
    return found


def ingredients_carrying(
    session: Session, menu_item_id: str, allergens: set[str]
) -> dict[str, list[str]]:
    """Map each requested allergen to the ingredients in this dish that carry it.

    This is what lets the order agent say *why* a dish is unsafe rather than
    just refusing it.
    """
    requirement = explode_menu_item(session, menu_item_id, 1)
    if not requirement:
        return {}
    ingredients = _load_ingredients(session, list(requirement))
    wanted = {a.strip().lower() for a in allergens if a.strip()}
    result: dict[str, list[str]] = {}
    for ing_id in requirement:
        ingredient = ingredients[ing_id]
        for allergen in ingredient.allergens:
            if allergen in wanted:
                result.setdefault(allergen, []).append(ingredient.name)
    return result


def explode_many(session: Session, quantities: dict[str, Decimal]) -> dict[str, Decimal]:
    """Aggregate ingredient requirement across a basket of menu items.

    Used by the prep forecaster to turn a per-item demand forecast into one
    consolidated shopping/prep list.
    """
    totals: dict[str, Decimal] = {}
    for menu_item_id, amount in quantities.items():
        if amount <= 0:
            continue
        for ing_id, required in explode_menu_item(session, menu_item_id, amount).items():
            totals[ing_id] = totals.get(ing_id, ZERO) + required
    return totals


def cost_of_requirement(session: Session, requirement: dict[str, Decimal]) -> Decimal:
    """Money value of an ingredient requirement, used for COGS and waste."""
    if not requirement:
        return ZERO
    ingredients = _load_ingredients(session, list(requirement))
    total = sum(
        (amount * ingredients[i].cost_per_base_unit for i, amount in requirement.items()), ZERO
    )
    return Decimal(total).quantize(Decimal("0.01"))


def _load_ingredients(session: Session, ingredient_ids: list[str]) -> dict[str, Ingredient]:
    if not ingredient_ids:
        return {}
    rows = session.execute(select(Ingredient).where(Ingredient.id.in_(ingredient_ids))).scalars()
    return {i.id: i for i in rows}
