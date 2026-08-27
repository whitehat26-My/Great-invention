import math
from datetime import date
from decimal import Decimal

import pytest

from restaurant_ai.domain.inventory import (
    StockPosition,
    SupplierPack,
    build_purchase_orders,
    days_of_cover,
    evaluate_position,
    next_delivery_date,
    reorder_point,
    safety_stock,
    target_level,
)

D = Decimal


def _pack(**overrides) -> SupplierPack:
    defaults = dict(
        stock_item_id="si-1",
        supplier_id="sup-1",
        supplier_code="SUP-A",
        supplier_name="Supplier A",
        supplier_sku="SKU-1",
        pack_size=D("1000"),
        pack_uom="kg",
        contract_price=D("50.00"),
        min_order_qty=D("1"),
        lead_time_days=2,
        min_order_value=D("0"),
        delivery_days=(0, 1, 2, 3, 4, 5, 6),
    )
    return SupplierPack(**{**defaults, **overrides})


def _position(**overrides) -> StockPosition:
    defaults = dict(
        ingredient_id="ing-1",
        code="ING-1",
        name="Test ingredient",
        base_uom="g",
        on_hand=D("100"),
        on_order=D("0"),
        avg_daily_usage=D("100"),
        usage_stddev=D("10"),
        lead_time_days=2,
        target_days_cover=7,
        shelf_life_days=30,
    )
    return StockPosition(**{**defaults, **overrides})


class TestSafetyStock:
    def test_matches_the_formula(self):
        # z * sigma * sqrt(lead time)
        expected = 1.65 * 12 * math.sqrt(2)
        assert float(safety_stock(D("12"), 2, 1.65)) == pytest.approx(expected, abs=0.01)

    def test_scales_with_sqrt_of_lead_time_not_linearly(self):
        # Quadrupling lead time doubles safety stock, it does not quadruple it.
        one = safety_stock(D("10"), 1, 1.65)
        four = safety_stock(D("10"), 4, 1.65)
        assert float(four) == pytest.approx(float(one * 2), abs=0.01)

    def test_higher_service_level_means_more_buffer(self):
        assert safety_stock(D("10"), 2, 2.33) > safety_stock(D("10"), 2, 1.65)

    def test_zero_variability_needs_no_buffer(self):
        assert safety_stock(D("0"), 5, 1.65) == D("0")

    def test_zero_lead_time(self):
        assert safety_stock(D("10"), 0, 1.65) == D("0")


class TestReorderPoint:
    def test_is_lead_demand_plus_safety(self):
        rop = reorder_point(D("100"), 2, D("12"), 1.65)
        assert rop == D("200") + safety_stock(D("12"), 2, 1.65)

    def test_no_variability_is_just_lead_demand(self):
        assert reorder_point(D("50"), 3, D("0")) == D("150")


class TestTargetLevel:
    def test_covers_target_plus_lead_time(self):
        target = target_level(D("100"), 7, 2, D("0"))
        assert target == D("900")

    def test_capped_by_shelf_life(self):
        # 7 days of cover for something that spoils in 3 would go in the bin.
        fresh = target_level(D("100"), 7, 2, D("0"), shelf_life_days=3)
        assert fresh == D("400")  # (3-1) days cover + 2 lead

    def test_long_shelf_life_is_not_capped(self):
        assert target_level(D("100"), 7, 2, D("0"), shelf_life_days=365) == D("900")


class TestEvaluatePosition:
    def test_no_suggestion_when_well_stocked(self):
        position = _position(on_hand=D("5000"))
        assert evaluate_position(position, _pack()) is None

    def test_suggests_when_below_reorder_point(self):
        suggestion = evaluate_position(_position(on_hand=D("50")), _pack())
        assert suggestion is not None
        assert suggestion.packs_to_order > 0

    def test_counts_stock_already_on_order(self):
        # Enough inbound to clear the reorder point means no new order.
        position = _position(on_hand=D("50"), on_order=D("5000"))
        assert evaluate_position(position, _pack()) is None

    def test_rounds_up_to_whole_packs(self):
        # Never order a fraction of a box.
        suggestion = evaluate_position(_position(on_hand=D("0")), _pack(pack_size=D("300")))
        assert suggestion.packs_to_order == suggestion.packs_to_order.to_integral_value()

    def test_respects_minimum_order_quantity(self):
        # on_hand must sit below the reorder point for the line to trigger at all.
        suggestion = evaluate_position(
            _position(on_hand=D("1"), avg_daily_usage=D("1"), usage_stddev=D("0")),
            _pack(pack_size=D("1000"), min_order_qty=D("5")),
        )
        assert suggestion is not None
        # One pack would more than cover demand, but the supplier will not sell
        # fewer than five.
        assert suggestion.packs_to_order == D("5")

    def test_zero_on_hand_is_a_stockout(self):
        assert evaluate_position(_position(on_hand=D("0")), _pack()).urgency == "stockout"

    def test_cover_shorter_than_lead_time_is_critical(self):
        # 0.5 days of stock with a 2-day lead time guarantees running out,
        # whatever the safety stock says.
        position = _position(on_hand=D("50"), avg_daily_usage=D("100"), usage_stddev=D("1"))
        assert evaluate_position(position, _pack(lead_time_days=2)).urgency == "critical"

    def test_rationale_explains_the_decision(self):
        suggestion = evaluate_position(_position(on_hand=D("50")), _pack())
        for fragment in ("on hand", "reorder point", "days cover", "lead time"):
            assert fragment in suggestion.rationale

    def test_rationale_reports_the_effective_cover_when_capped(self):
        suggestion = evaluate_position(
            _position(on_hand=D("50"), shelf_life_days=3, target_days_cover=7), _pack()
        )
        assert "capped from 7d by 3d shelf life" in suggestion.rationale
        assert "2d cover" in suggestion.rationale

    def test_line_cost_is_packs_times_price(self):
        suggestion = evaluate_position(
            _position(on_hand=D("0")), _pack(pack_size=D("1000"), contract_price=D("25.00"))
        )
        assert suggestion.line_cost == suggestion.packs_to_order * D("25.00")


class TestDeliveryDates:
    def test_respects_lead_time(self):
        assert next_delivery_date(date(2026, 8, 27), 2, (0, 1, 2, 3, 4, 5, 6)) == date(2026, 8, 29)

    def test_skips_to_the_next_delivery_day(self):
        # Thursday + 2 days lead = Saturday, but this supplier delivers Mon/Wed/Fri.
        result = next_delivery_date(date(2026, 8, 27), 2, (0, 2, 4))
        assert result.weekday() in (0, 2, 4)
        assert result >= date(2026, 8, 29)


class TestBuildPurchaseOrders:
    def test_groups_by_supplier(self):
        a = evaluate_position(_position(on_hand=D("0")), _pack(supplier_id="s1"))
        b = evaluate_position(
            _position(ingredient_id="ing-2", on_hand=D("0")), _pack(supplier_id="s2")
        )
        drafts = build_purchase_orders([a, b], date(2026, 8, 27))
        assert len(drafts) == 2

    def test_one_draft_per_supplier_with_multiple_lines(self):
        lines = [
            evaluate_position(_position(ingredient_id=f"ing-{i}", on_hand=D("0")), _pack())
            for i in range(3)
        ]
        drafts = build_purchase_orders(lines, date(2026, 8, 27))
        assert len(drafts) == 1
        assert len(drafts[0].lines) == 3

    def test_flags_below_minimum_order_value(self):
        line = evaluate_position(
            _position(on_hand=D("0"), avg_daily_usage=D("1"), usage_stddev=D("0")),
            _pack(contract_price=D("10.00"), min_order_value=D("500")),
        )
        draft = build_purchase_orders([line], date(2026, 8, 27))[0]
        assert draft.below_minimum
        assert any("minimum order" in note for note in draft.notes)

    def test_below_minimum_is_surfaced_not_suppressed(self):
        # Dropping the order silently would hide a genuine stockout risk.
        line = evaluate_position(
            _position(on_hand=D("0"), avg_daily_usage=D("1"), usage_stddev=D("0")),
            _pack(contract_price=D("1.00"), min_order_value=D("999")),
        )
        drafts = build_purchase_orders([line], date(2026, 8, 27))
        assert len(drafts) == 1 and len(drafts[0].lines) == 1

    def test_subtotal_sums_lines(self):
        lines = [
            evaluate_position(_position(ingredient_id=f"ing-{i}", on_hand=D("0")), _pack())
            for i in range(3)
        ]
        draft = build_purchase_orders(lines, date(2026, 8, 27))[0]
        assert draft.subtotal == sum(line.line_cost for line in draft.lines)

    def test_notes_call_out_stockouts(self):
        line = evaluate_position(_position(on_hand=D("0")), _pack())
        draft = build_purchase_orders([line], date(2026, 8, 27))[0]
        assert any("zero on hand" in note for note in draft.notes)

    def test_empty_input(self):
        assert build_purchase_orders([], date(2026, 8, 27)) == []


def test_days_of_cover():
    assert days_of_cover(D("500"), D("100")) == D("5.0")
    assert days_of_cover(D("100"), D("0")) == D("999")
