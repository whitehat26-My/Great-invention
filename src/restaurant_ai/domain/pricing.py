"""Menu engineering and price proposals.

Classic menu-engineering: plot every item on popularity against contribution
margin and you get four quadrants, each with a different correct action.

    Star      high popularity, high margin  -> protect, feature, never discount
    Plowhorse high popularity, low margin   -> raise price or re-engineer cost
    Puzzle    low popularity, high margin   -> promote, reposition on the menu
    Dog       low popularity, low margin    -> bundle, rework, or delist

Price changes are then bounded by guardrails, because an agent with unrestricted
pricing authority is how you wake up to a menu that has quietly doubled. Every
proposal is capped in size, rate-limited by a cooldown, and floored on margin.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from restaurant_ai.db.models.enums import MenuClass

ZERO = Decimal("0")


@dataclass
class ItemPerformance:
    """One item's trading record over the analysis window."""

    menu_item_id: str
    sku: str
    name: str
    price: Decimal
    unit_cost: Decimal
    units_sold: Decimal
    section: str = ""
    last_price_change_on: date | None = None

    @property
    def contribution_margin(self) -> Decimal:
        return (self.price - self.unit_cost).quantize(Decimal("0.01"))

    @property
    def margin_pct(self) -> Decimal:
        if self.price <= 0:
            return ZERO
        return ((self.price - self.unit_cost) / self.price).quantize(Decimal("0.0001"))

    @property
    def total_contribution(self) -> Decimal:
        return (self.contribution_margin * self.units_sold).quantize(Decimal("0.01"))

    @property
    def revenue(self) -> Decimal:
        return (self.price * self.units_sold).quantize(Decimal("0.01"))


@dataclass
class ClassifiedItem:
    performance: ItemPerformance
    menu_class: MenuClass
    popularity_index: Decimal  # 1.0 == average share of units
    margin_index: Decimal  # 1.0 == average contribution margin
    recommendation: str


@dataclass
class PriceProposal:
    menu_item_id: str
    sku: str
    name: str
    current_price: Decimal
    proposed_price: Decimal
    change_pct: Decimal
    menu_class: MenuClass
    projected_margin_pct: Decimal
    expected_unit_impact: Decimal
    expected_contribution_delta: Decimal
    rationale: str

    @property
    def is_increase(self) -> bool:
        return self.proposed_price > self.current_price


@dataclass
class BundleProposal:
    name: str
    menu_item_ids: list[str]
    component_names: list[str]
    list_price: Decimal
    bundle_price: Decimal
    discount_pct: Decimal
    bundle_cost: Decimal
    bundle_margin_pct: Decimal
    rationale: str


@dataclass
class MenuAnalysis:
    items: list[ClassifiedItem] = field(default_factory=list)
    avg_popularity: Decimal = ZERO
    avg_margin: Decimal = ZERO
    total_units: Decimal = ZERO
    total_contribution: Decimal = ZERO

    def by_class(self, menu_class: MenuClass) -> list[ClassifiedItem]:
        return [i for i in self.items if i.menu_class == menu_class]


# Quadrant -> what to actually do about it.
RECOMMENDATIONS: dict[MenuClass, str] = {
    MenuClass.STAR: (
        "Protect it. Keep the recipe and portion exactly as they are, feature it in "
        "photography and staff recommendations, and do not discount it in bundles."
    ),
    MenuClass.PLOWHORSE: (
        "Sells well but earns little. Either take a modest price rise (demand is proven) "
        "or re-engineer the plate cost by portioning the expensive component more tightly."
    ),
    MenuClass.PUZZLE: (
        "Earns well but few order it. Move it up the menu, give it a better description, "
        "and have servers suggest it before touching the price."
    ),
    MenuClass.DOG: (
        "Neither popular nor profitable. Rework it, fold it into a bundle to shift the "
        "stock it consumes, or delist it and free the prep time."
    ),
}


def classify_menu(
    performances: list[ItemPerformance],
    popularity_threshold: Decimal = Decimal("0.70"),
) -> MenuAnalysis:
    """Sort the menu into the four quadrants.

    Popularity is measured against an equal-share baseline scaled by
    ``popularity_threshold`` (the conventional 70% rule), not against the mean —
    with a few runaway sellers the mean is dragged up and almost everything
    looks unpopular.
    """
    active = [p for p in performances if p.price > 0]
    if not active:
        return MenuAnalysis()

    total_units = sum((p.units_sold for p in active), ZERO)
    total_contribution = sum((p.total_contribution for p in active), ZERO)
    equal_share = Decimal("1") / Decimal(len(active))
    popularity_bar = equal_share * popularity_threshold

    margins = [p.contribution_margin for p in active]
    avg_margin = (sum(margins, ZERO) / Decimal(len(margins))).quantize(Decimal("0.01"))

    classified: list[ClassifiedItem] = []
    for item in active:
        share = (item.units_sold / total_units) if total_units > 0 else ZERO
        popular = share >= popularity_bar
        profitable = item.contribution_margin >= avg_margin

        if popular and profitable:
            menu_class = MenuClass.STAR
        elif popular:
            menu_class = MenuClass.PLOWHORSE
        elif profitable:
            menu_class = MenuClass.PUZZLE
        else:
            menu_class = MenuClass.DOG

        classified.append(
            ClassifiedItem(
                performance=item,
                menu_class=menu_class,
                popularity_index=(share / equal_share).quantize(Decimal("0.01"))
                if equal_share > 0
                else ZERO,
                margin_index=(item.contribution_margin / avg_margin).quantize(Decimal("0.01"))
                if avg_margin > 0
                else ZERO,
                recommendation=RECOMMENDATIONS[menu_class],
            )
        )

    return MenuAnalysis(
        items=sorted(classified, key=lambda c: c.performance.total_contribution, reverse=True),
        avg_popularity=equal_share.quantize(Decimal("0.0001")),
        avg_margin=avg_margin,
        total_units=total_units,
        total_contribution=total_contribution.quantize(Decimal("0.01")),
    )


def propose_price_changes(
    analysis: MenuAnalysis,
    today: date,
    max_change_pct: Decimal = Decimal("0.10"),
    cooldown_days: int = 14,
    min_margin_pct: Decimal = Decimal("0.55"),
    price_elasticity: Decimal = Decimal("-1.2"),
    max_proposals: int = 5,
) -> list[PriceProposal]:
    """Propose bounded price changes for the items where they are justified.

    Elasticity of -1.2 is a reasonable default for casual dining: a 10% price
    rise costs roughly 12% of units. Any proposal whose projected contribution
    falls is dropped, so a rise is only suggested when the margin gain genuinely
    beats the volume loss.
    """
    proposals: list[PriceProposal] = []

    for entry in analysis.items:
        item = entry.performance

        if item.last_price_change_on is not None:
            days_since = (today - item.last_price_change_on).days
            if days_since < cooldown_days:
                continue

        direction: Decimal | None = None
        reason = ""

        if entry.menu_class == MenuClass.PLOWHORSE and item.margin_pct < min_margin_pct:
            direction = max_change_pct
            reason = (
                f"Plowhorse: {entry.popularity_index}x average popularity but a "
                f"{item.margin_pct * 100:.1f}% margin, under the {min_margin_pct * 100:.0f}% floor. "
                f"Demand is proven, so a rise is the lower-risk lever."
            )
        elif entry.menu_class == MenuClass.DOG and item.margin_pct < min_margin_pct:
            direction = max_change_pct / 2
            reason = (
                f"Dog: {entry.popularity_index}x popularity and below-average margin. "
                f"A small rise recovers margin on the few that do sell; if volume falls "
                f"further, that is the delist signal."
            )
        elif entry.menu_class == MenuClass.PUZZLE and item.margin_pct > min_margin_pct + Decimal(
            "0.15"
        ):
            direction = -max_change_pct / 2
            reason = (
                f"Puzzle: strong {item.margin_pct * 100:.1f}% margin but only "
                f"{entry.popularity_index}x popularity. Headroom exists to trade a little "
                f"margin for trial and see if volume responds."
            )

        if direction is None or direction == 0:
            continue

        proposed = _round_price(item.price * (Decimal("1") + direction))
        if proposed == item.price:
            continue

        actual_change = ((proposed - item.price) / item.price).quantize(Decimal("0.0001"))
        projected_margin_pct = (
            ((proposed - item.unit_cost) / proposed).quantize(Decimal("0.0001"))
            if proposed > 0
            else ZERO
        )

        unit_impact = (item.units_sold * actual_change * price_elasticity).quantize(Decimal("0.01"))
        projected_units = item.units_sold + unit_impact
        current_contribution = item.total_contribution
        projected_contribution = ((proposed - item.unit_cost) * projected_units).quantize(
            Decimal("0.01")
        )
        delta = (projected_contribution - current_contribution).quantize(Decimal("0.01"))

        if delta <= 0:
            continue

        proposals.append(
            PriceProposal(
                menu_item_id=item.menu_item_id,
                sku=item.sku,
                name=item.name,
                current_price=item.price,
                proposed_price=proposed,
                change_pct=actual_change,
                menu_class=entry.menu_class,
                projected_margin_pct=projected_margin_pct,
                expected_unit_impact=unit_impact,
                expected_contribution_delta=delta,
                rationale=(
                    f"{reason} Proposing {item.price} -> {proposed} "
                    f"({actual_change * 100:+.1f}%). At an elasticity of {price_elasticity}, "
                    f"units move {unit_impact:+.0f} but contribution improves by {delta:+.2f} "
                    f"over the window."
                ),
            )
        )

    proposals.sort(key=lambda p: p.expected_contribution_delta, reverse=True)
    return proposals[:max_proposals]


def propose_bundles(
    analysis: MenuAnalysis,
    discount_pct: Decimal = Decimal("0.12"),
    max_bundles: int = 3,
    min_margin_pct: Decimal = Decimal("0.55"),
) -> list[BundleProposal]:
    """Pair slow movers with proven sellers.

    A bundle only makes sense if it stays profitable after the discount, so each
    candidate is checked against the blended cost before being proposed.

    The floor used to be a hardcoded 40% while the agent reported the
    configured 55% alongside the results — so a bundle at 54.6% was published
    under a guardrail that had never been applied to it. A report claiming a
    constraint was enforced when it was not is worse than no report.
    """
    stars = analysis.by_class(MenuClass.STAR)
    laggards = analysis.by_class(MenuClass.DOG) + analysis.by_class(MenuClass.PUZZLE)
    if not stars or not laggards:
        return []

    bundles: list[BundleProposal] = []
    for laggard in sorted(laggards, key=lambda c: c.performance.units_sold)[:max_bundles]:
        anchor = stars[0]
        components = [anchor.performance, laggard.performance]
        list_price = sum((c.price for c in components), ZERO)
        bundle_price = _round_price(list_price * (Decimal("1") - discount_pct))
        bundle_cost = sum((c.unit_cost for c in components), ZERO)
        margin_pct = (
            ((bundle_price - bundle_cost) / bundle_price).quantize(Decimal("0.0001"))
            if bundle_price > 0
            else ZERO
        )
        if margin_pct < min_margin_pct:
            continue

        bundles.append(
            BundleProposal(
                name=f"{anchor.performance.name} + {laggard.performance.name}",
                menu_item_ids=[c.menu_item_id for c in components],
                component_names=[c.name for c in components],
                list_price=list_price.quantize(Decimal("0.01")),
                bundle_price=bundle_price,
                discount_pct=discount_pct,
                bundle_cost=bundle_cost.quantize(Decimal("0.01")),
                bundle_margin_pct=margin_pct,
                rationale=(
                    f"Pairs {laggard.performance.name} "
                    f"({laggard.menu_class.value}, {laggard.popularity_index}x popularity) with "
                    f"{anchor.performance.name}, the strongest seller. At {bundle_price} the "
                    f"bundle still holds a {margin_pct * 100:.1f}% margin while moving stock "
                    f"that is otherwise sitting."
                ),
            )
        )
    return bundles


def _round_price(value: Decimal) -> Decimal:
    """Round to a charm price ending in .90, the convention on this menu."""
    if value <= 0:
        return ZERO
    whole = int(value)
    frac = value - whole
    if frac < Decimal("0.45"):
        return Decimal(whole) - Decimal("0.10") if whole >= 1 else Decimal("0.90")
    return Decimal(whole) + Decimal("0.90")
