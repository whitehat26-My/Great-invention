from datetime import date, timedelta
from decimal import Decimal

from restaurant_ai.db.models.enums import MenuClass
from restaurant_ai.domain.pricing import (
    ItemPerformance,
    classify_menu,
    propose_bundles,
    propose_price_changes,
)

D = Decimal
TODAY = date(2026, 8, 27)


def _perf(sku, price, cost, units, changed=None) -> ItemPerformance:
    return ItemPerformance(
        menu_item_id=f"id-{sku}",
        sku=sku,
        name=f"Item {sku}",
        price=D(price),
        unit_cost=D(cost),
        units_sold=D(units),
        last_price_change_on=changed,
    )


class TestClassify:
    def test_four_quadrants(self):
        items = [
            _perf("STAR", "30.00", "9.00", "300"),  # popular, high margin
            _perf("PLOW", "12.00", "9.00", "300"),  # popular, low margin
            _perf("PUZZ", "30.00", "9.00", "10"),  # unpopular, high margin
            _perf("DOG", "12.00", "9.00", "10"),  # unpopular, low margin
        ]
        by_sku = {c.performance.sku: c.menu_class for c in classify_menu(items).items}
        assert by_sku["STAR"] == MenuClass.STAR
        assert by_sku["PLOW"] == MenuClass.PLOWHORSE
        assert by_sku["PUZZ"] == MenuClass.PUZZLE
        assert by_sku["DOG"] == MenuClass.DOG

    def test_popularity_uses_equal_share_not_mean(self):
        # One runaway seller drags the mean up; measuring against it would
        # misclassify perfectly healthy items as unpopular.
        items = [_perf("HUGE", "20.00", "6.00", "10000")] + [
            _perf(f"OK{i}", "20.00", "6.00", "100") for i in range(4)
        ]
        analysis = classify_menu(items)
        ok_items = [c for c in analysis.items if c.performance.sku.startswith("OK")]
        assert all(c.popularity_index > 0 for c in ok_items)

    def test_contribution_margin_and_percentages(self):
        item = _perf("X", "25.00", "10.00", "100")
        assert item.contribution_margin == D("15.00")
        assert item.margin_pct == D("0.6000")
        assert item.total_contribution == D("1500.00")

    def test_every_class_carries_an_action(self):
        items = [
            _perf("STAR", "30.00", "9.00", "300"),
            _perf("PLOW", "12.00", "9.00", "300"),
            _perf("PUZZ", "30.00", "9.00", "10"),
            _perf("DOG", "12.00", "9.00", "10"),
        ]
        assert all(len(c.recommendation) > 30 for c in classify_menu(items).items)

    def test_empty_menu(self):
        assert classify_menu([]).items == []

    def test_zero_priced_items_are_excluded(self):
        assert classify_menu([_perf("FREE", "0", "0", "10")]).items == []


class TestPriceProposals:
    def test_low_margin_plowhorse_gets_a_rise(self):
        items = [
            _perf("PLOW", "12.00", "9.00", "300"),
            _perf("OTHER", "30.00", "9.00", "300"),
        ]
        proposals = propose_price_changes(classify_menu(items), TODAY)
        plow = [p for p in proposals if p.sku == "PLOW"]
        assert plow and plow[0].proposed_price > D("12.00")

    def test_change_is_capped(self):
        items = [_perf("PLOW", "12.00", "11.00", "300"), _perf("O", "30.00", "9.00", "300")]
        for p in propose_price_changes(classify_menu(items), TODAY, max_change_pct=D("0.10")):
            assert abs(p.change_pct) <= D("0.11")  # rounding to .90 allows slight overshoot

    def test_cooldown_blocks_a_recent_change(self):
        recent = TODAY - timedelta(days=3)
        items = [
            _perf("PLOW", "12.00", "9.00", "300", changed=recent),
            _perf("O", "30.00", "9.00", "300"),
        ]
        proposals = propose_price_changes(classify_menu(items), TODAY, cooldown_days=14)
        assert not [p for p in proposals if p.sku == "PLOW"]

    def test_cooldown_expiry_allows_a_change(self):
        old = TODAY - timedelta(days=60)
        items = [
            _perf("PLOW", "12.00", "9.00", "300", changed=old),
            _perf("O", "30.00", "9.00", "300"),
        ]
        proposals = propose_price_changes(classify_menu(items), TODAY, cooldown_days=14)
        assert [p for p in proposals if p.sku == "PLOW"]

    def test_only_proposes_when_contribution_improves(self):
        # Every surviving proposal must beat the volume loss it causes.
        items = [_perf("PLOW", "12.00", "9.00", "300"), _perf("O", "30.00", "9.00", "300")]
        proposals = propose_price_changes(classify_menu(items), TODAY)
        assert all(p.expected_contribution_delta > 0 for p in proposals)

    def test_elastic_demand_suppresses_rises(self):
        # At very high elasticity a price rise loses more volume than it gains
        # in margin, so nothing should be proposed.
        items = [_perf("PLOW", "12.00", "9.00", "300"), _perf("O", "30.00", "9.00", "300")]
        proposals = propose_price_changes(classify_menu(items), TODAY, price_elasticity=D("-9.0"))
        assert proposals == []

    def test_respects_max_proposals(self):
        items = [_perf(f"P{i}", "12.00", "9.00", "300") for i in range(10)]
        items.append(_perf("HIGH", "40.00", "5.00", "300"))
        proposals = propose_price_changes(classify_menu(items), TODAY, max_proposals=3)
        assert len(proposals) <= 3

    def test_prices_land_on_charm_endings(self):
        items = [_perf("PLOW", "12.00", "9.00", "300"), _perf("O", "30.00", "9.00", "300")]
        for p in propose_price_changes(classify_menu(items), TODAY):
            assert str(p.proposed_price).endswith(".90")

    def test_rationale_explains_the_proposal(self):
        items = [_perf("PLOW", "12.00", "9.00", "300"), _perf("O", "30.00", "9.00", "300")]
        for p in propose_price_changes(classify_menu(items), TODAY):
            assert "elasticity" in p.rationale and "contribution" in p.rationale

    def test_sorted_by_impact(self):
        items = [_perf(f"P{i}", f"{10 + i}.00", "9.00", str(300 + i * 10)) for i in range(6)]
        items.append(_perf("HIGH", "40.00", "5.00", "300"))
        proposals = propose_price_changes(classify_menu(items), TODAY, max_proposals=10)
        deltas = [p.expected_contribution_delta for p in proposals]
        assert deltas == sorted(deltas, reverse=True)


class TestBundles:
    def test_pairs_a_laggard_with_a_star(self):
        items = [
            _perf("STAR", "30.00", "8.00", "500"),
            _perf("DOG", "14.00", "5.00", "5"),
        ]
        bundles = propose_bundles(classify_menu(items))
        assert bundles
        assert "STAR" in " ".join(bundles[0].menu_item_ids)

    def test_bundle_stays_profitable(self):
        items = [_perf("STAR", "30.00", "8.00", "500"), _perf("DOG", "14.00", "5.00", "5")]
        for bundle in propose_bundles(classify_menu(items)):
            assert bundle.bundle_margin_pct >= D("0.55")

    def test_thin_margin_bundles_are_rejected(self):
        items = [_perf("STAR", "20.00", "13.00", "500"), _perf("DOG", "14.00", "11.00", "5")]
        assert propose_bundles(classify_menu(items), discount_pct=D("0.30")) == []

    def test_the_floor_is_the_one_it_was_given(self):
        """It was hardcoded at 40% while the agent reported the configured 55%.

        So a bundle at 54.6% went out under a guardrail that had never been
        applied to it — and the run said "completed". A report claiming a
        constraint was enforced when it was not is worse than no report.
        """
        items = [_perf("STAR", "30.00", "11.00", "500"), _perf("DOG", "14.00", "6.00", "5")]

        lenient = propose_bundles(classify_menu(items), min_margin_pct=D("0.40"))
        strict = propose_bundles(classify_menu(items), min_margin_pct=D("0.99"))

        assert lenient, "a 40% floor should admit this pair"
        assert strict == [], "a 99% floor must admit nothing"
        for bundle in lenient:
            assert bundle.bundle_margin_pct >= D("0.40")

    def test_the_default_floor_matches_the_configured_one(self):
        # The agent reports `min_gross_margin_pct` as the guardrail it applied.
        import inspect

        from restaurant_ai.config import get_settings

        default = inspect.signature(propose_bundles).parameters["min_margin_pct"].default
        assert default == get_settings().min_gross_margin_pct

    def test_needs_both_a_star_and_a_laggard(self):
        assert propose_bundles(classify_menu([_perf("ONLY", "30.00", "8.00", "500")])) == []

    def test_bundle_price_is_discounted(self):
        items = [_perf("STAR", "30.00", "8.00", "500"), _perf("DOG", "14.00", "5.00", "5")]
        for bundle in propose_bundles(classify_menu(items)):
            assert bundle.bundle_price < bundle.list_price
