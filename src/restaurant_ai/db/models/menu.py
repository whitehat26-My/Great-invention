"""Menu and recipe bill-of-materials.

The BOM is the backbone of the whole platform: it is what turns a POS sale into
ingredient deductions, a menu item into a plate cost, and a prep forecast into a
shopping list. ``recipe_component`` is self-referencing so a recipe can consume
either a raw ingredient or another recipe (a sub-recipe such as a sauce or dough).
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from restaurant_ai.db.base import Base, Money, Qty, Timestamped, UUIDPk
from restaurant_ai.db.models.enums import MenuClass, Station


class MenuSection(UUIDPk, Timestamped, Base):
    __tablename__ = "menu_section"

    name: Mapped[str] = mapped_column(String(80), unique=True)
    display_order: Mapped[int] = mapped_column(Integer, default=0)

    items: Mapped[list[MenuItem]] = relationship(back_populates="section")


class MenuItem(UUIDPk, Timestamped, Base):
    __tablename__ = "menu_item"

    sku: Mapped[str] = mapped_column(String(40), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(160))
    description: Mapped[str | None] = mapped_column(Text)
    section_id: Mapped[str] = mapped_column(ForeignKey("menu_section.id"))
    price: Mapped[Decimal] = mapped_column(Money)
    tax_rate: Mapped[Decimal] = mapped_column(Money, default=Decimal("0.06"))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    station: Mapped[Station] = mapped_column(
        Enum(Station, native_enum=False, length=20), default=Station.SAUTE
    )
    # Nominal prep time; the pacing agent uses this to sequence courses.
    prep_seconds: Mapped[int] = mapped_column(Integer, default=480)
    course: Mapped[int] = mapped_column(Integer, default=2, doc="1=starter 2=main 3=dessert")
    menu_class: Mapped[MenuClass | None] = mapped_column(
        Enum(MenuClass, native_enum=False, length=20)
    )
    last_price_change_on: Mapped[date | None] = mapped_column(Date)

    section: Mapped[MenuSection] = relationship(back_populates="items")
    recipe: Mapped[Recipe | None] = relationship(back_populates="menu_item", uselist=False)
    price_history: Mapped[list[MenuItemPriceHistory]] = relationship(
        back_populates="menu_item", order_by="MenuItemPriceHistory.effective_from.desc()"
    )

    __table_args__ = (CheckConstraint("price >= 0", name="price_non_negative"),)


class MenuItemPriceHistory(UUIDPk, Timestamped, Base):
    """Append-only price log, so elasticity tests have a before/after to compare."""

    __tablename__ = "menu_item_price_history"

    menu_item_id: Mapped[str] = mapped_column(ForeignKey("menu_item.id"), index=True)
    old_price: Mapped[Decimal] = mapped_column(Money)
    new_price: Mapped[Decimal] = mapped_column(Money)
    effective_from: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    reason: Mapped[str | None] = mapped_column(Text)
    changed_by: Mapped[str] = mapped_column(String(80), default="menu_pricing_agent")

    menu_item: Mapped[MenuItem] = relationship(back_populates="price_history")


class Allergen(UUIDPk, Base):
    __tablename__ = "allergen"

    code: Mapped[str] = mapped_column(String(30), unique=True)
    label: Mapped[str] = mapped_column(String(80))


class Ingredient(UUIDPk, Timestamped, Base):
    """A purchasable raw good. Costing and stock both hang off this."""

    __tablename__ = "ingredient"

    code: Mapped[str] = mapped_column(String(40), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(160))
    base_uom: Mapped[str] = mapped_column(String(12), doc="g, ml, ea")
    # Cost per base unit; refreshed by the invoice agent as prices move.
    cost_per_base_unit: Mapped[Decimal] = mapped_column(Qty, default=Decimal("0"))
    # Fraction lost to trim/peel/cook-off. Prep quantities are grossed up by this.
    yield_pct: Mapped[Decimal] = mapped_column(Qty, default=Decimal("1.0"))
    shelf_life_days: Mapped[int] = mapped_column(Integer, default=7)
    allergen_codes: Mapped[str] = mapped_column(
        String(300), default="", doc="Comma-separated allergen codes."
    )

    __table_args__ = (
        CheckConstraint("yield_pct > 0 AND yield_pct <= 1", name="yield_pct_fraction"),
    )

    @property
    def allergens(self) -> list[str]:
        return [a for a in (self.allergen_codes or "").split(",") if a]


class Recipe(UUIDPk, Timestamped, Base):
    """A recipe either plates a menu item or is a sub-recipe consumed by others."""

    __tablename__ = "recipe"

    code: Mapped[str] = mapped_column(String(40), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(160))
    menu_item_id: Mapped[str | None] = mapped_column(
        ForeignKey("menu_item.id"), unique=True, nullable=True
    )
    # A batch yields this many units of `yield_uom` (e.g. 2000 ml of sauce).
    yield_qty: Mapped[Decimal] = mapped_column(Qty, default=Decimal("1"))
    yield_uom: Mapped[str] = mapped_column(String(12), default="ea")
    station: Mapped[Station] = mapped_column(
        Enum(Station, native_enum=False, length=20), default=Station.SAUTE
    )
    prep_seconds: Mapped[int] = mapped_column(Integer, default=300)
    method: Mapped[str | None] = mapped_column(Text)

    menu_item: Mapped[MenuItem | None] = relationship(back_populates="recipe")
    components: Mapped[list[RecipeComponent]] = relationship(
        back_populates="recipe",
        foreign_keys="RecipeComponent.recipe_id",
        cascade="all, delete-orphan",
    )

    __table_args__ = (CheckConstraint("yield_qty > 0", name="yield_qty_positive"),)


class RecipeComponent(UUIDPk, Base):
    """One line of a BOM: exactly one of ingredient_id / sub_recipe_id is set."""

    __tablename__ = "recipe_component"

    recipe_id: Mapped[str] = mapped_column(ForeignKey("recipe.id"), index=True)
    ingredient_id: Mapped[str | None] = mapped_column(ForeignKey("ingredient.id"))
    sub_recipe_id: Mapped[str | None] = mapped_column(ForeignKey("recipe.id"))
    quantity: Mapped[Decimal] = mapped_column(Qty)
    uom: Mapped[str] = mapped_column(String(12))
    note: Mapped[str | None] = mapped_column(String(200))

    recipe: Mapped[Recipe] = relationship(back_populates="components", foreign_keys=[recipe_id])
    ingredient: Mapped[Ingredient | None] = relationship()
    sub_recipe: Mapped[Recipe | None] = relationship(foreign_keys=[sub_recipe_id])

    __table_args__ = (
        CheckConstraint(
            "(ingredient_id IS NOT NULL) <> (sub_recipe_id IS NOT NULL)",
            name="exactly_one_target",
        ),
        CheckConstraint("quantity > 0", name="quantity_positive"),
        CheckConstraint("sub_recipe_id IS NULL OR sub_recipe_id <> recipe_id", name="no_self_ref"),
        Index("ix_recipe_component_recipe_ingredient", "recipe_id", "ingredient_id"),
    )


class UomConversion(UUIDPk, Base):
    """Unit conversions, optionally scoped to one ingredient (e.g. 1 ea egg = 50 g)."""

    __tablename__ = "uom_conversion"

    from_uom: Mapped[str] = mapped_column(String(12))
    to_uom: Mapped[str] = mapped_column(String(12))
    factor: Mapped[Decimal] = mapped_column(Qty, doc="qty_in_to_uom = qty_in_from_uom * factor")
    ingredient_id: Mapped[str | None] = mapped_column(ForeignKey("ingredient.id"))

    __table_args__ = (
        UniqueConstraint("from_uom", "to_uom", "ingredient_id", name="uq_uom_conversion_triple"),
        CheckConstraint("factor > 0", name="factor_positive"),
    )
