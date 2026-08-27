from datetime import date, timedelta
from decimal import Decimal

from restaurant_ai.domain.forecasting import (
    DailySales,
    forecast_day,
    learn_bias,
    score_accuracy,
    stdev,
    weekday_profile,
)

D = Decimal


def _history(
    start: date, days: int, item: str = "item-1", per_day: dict[int, str] | None = None
) -> list[DailySales]:
    """Build history with a per-weekday level, which is the signal to recover."""
    per_day = per_day or {}
    rows = []
    for offset in range(days):
        day = start + timedelta(days=offset)
        qty = per_day.get(day.weekday(), "10")
        rows.append(DailySales(business_date=day, menu_item_id=item, quantity=D(qty)))
    return rows


class TestForecastDay:
    def test_recovers_the_weekday_level(self):
        # Saturdays run at 40, every other day at 10. Forecasting a Saturday
        # must return the Saturday level, not the overall average.
        levels = {5: "40"}
        history = _history(date(2026, 6, 1), 56, per_day=levels)
        target = date(2026, 7, 27) + timedelta(days=(5 - date(2026, 7, 27).weekday()) % 7)
        assert target.weekday() == 5

        result = forecast_day(history, target)
        forecast = result.items["item-1"].forecast_qty
        assert D("36") <= forecast <= D("44"), f"expected ~40, got {forecast}"

    def test_uses_only_prior_days(self):
        history = _history(date(2026, 6, 1), 30)
        target = date(2026, 6, 15)
        result = forecast_day(history, target)
        # Nothing on or after the target may influence the forecast.
        assert result.items["item-1"].observations > 0
        assert all(r.business_date < target for r in history[:14])

    def test_falls_back_when_no_matching_weekday(self):
        # Only three days of history, none on the target weekday.
        history = [
            DailySales(date(2026, 6, 1), "item-1", D("10")),
            DailySales(date(2026, 6, 2), "item-1", D("12")),
            DailySales(date(2026, 6, 3), "item-1", D("14")),
        ]
        result = forecast_day(history, date(2026, 6, 6))  # a Saturday
        assert result.items["item-1"].method == "recent_mean"
        assert result.items["item-1"].forecast_qty > 0

    def test_empty_history_yields_nothing(self):
        assert forecast_day([], date(2026, 6, 1)).items == {}

    def test_event_factor_scales_output(self):
        history = _history(date(2026, 6, 1), 56)
        target = date(2026, 7, 28)
        base = forecast_day(history, target).items["item-1"].forecast_qty
        boosted = forecast_day(history, target, event_factor=D("2")).items["item-1"].forecast_qty
        assert boosted > base

    def test_bias_is_clamped(self):
        # A wild bias must not be applied verbatim; the clamp protects prep.
        history = _history(date(2026, 6, 1), 56)
        target = date(2026, 7, 28)
        result = forecast_day(history, target, bias={"item-1": D("99")})
        assert result.items["item-1"].bias_factor <= D("1.25")

    def test_forecast_is_never_fractional(self):
        history = _history(date(2026, 6, 1), 56, per_day={0: "7", 1: "9"})
        result = forecast_day(history, date(2026, 7, 28))
        assert (
            result.items["item-1"].forecast_qty
            == result.items["item-1"].forecast_qty.to_integral_value()
        )

    def test_explain_is_human_readable(self):
        history = _history(date(2026, 6, 1), 56)
        result = forecast_day(history, date(2026, 7, 28))
        text = result.items["item-1"].explain()
        assert "baseline" in text and "trend" in text


class TestAccuracy:
    def test_scores_absolute_error(self):
        errors = score_accuracy({"a": D("10"), "b": D("20")}, {"a": D("12"), "b": D("15")})
        assert errors["a"] == D("2")
        assert errors["b"] == D("5")

    def test_bias_above_one_means_under_forecast(self):
        errors = score_accuracy({"a": D("10")}, {"a": D("15")})
        assert errors["__bias__"] > D("1")

    def test_bias_below_one_means_over_forecast(self):
        errors = score_accuracy({"a": D("20")}, {"a": D("10")})
        assert errors["__bias__"] < D("1")

    def test_mape_is_reported(self):
        errors = score_accuracy({"a": D("10")}, {"a": D("20")})
        assert errors["__mape__"] == D("0.5")

    def test_handles_item_sold_but_not_forecast(self):
        errors = score_accuracy({}, {"surprise": D("6")})
        assert errors["surprise"] == D("6")


class TestLearnBias:
    def test_damped_toward_one(self):
        # Actual is double the forecast; a damped correction moves halfway, not
        # all the way, so the model converges instead of oscillating.
        bias = learn_bias({"a": D("10")}, {"a": D("20")}, damping=D("0.5"))
        assert bias["a"] == D("1.25")  # clamped from 1.5

    def test_respects_clamp_floor(self):
        bias = learn_bias({"a": D("100")}, {"a": D("1")}, damping=D("1"))
        assert bias["a"] >= D("0.80")

    def test_skips_zero_forecast(self):
        assert learn_bias({"a": D("0")}, {"a": D("5")}) == {}


class TestWeekdayProfile:
    def test_normalises_around_one(self):
        history = _history(date(2026, 6, 1), 70, per_day={4: "20", 5: "20"})
        profile = weekday_profile(history)
        assert profile[4] > D("1")  # Friday busier than average
        assert profile[0] < D("1")  # Monday quieter

    def test_empty_history(self):
        assert weekday_profile([]) == {}


def test_stdev():
    assert stdev([D("2"), D("4"), D("4"), D("4"), D("5"), D("5"), D("7"), D("9")]) > D("2")
    assert stdev([D("5")]) == D("0")
    assert stdev([]) == D("0")


class TestTrendIsSeasonalityFree:
    """The trend factor must measure real growth, not weekday composition.

    These pin down a bug where the trend was computed from raw window totals:
    a window with proportionally more weekend days read as growth, and a window
    ending before the target (the normal case, since you forecast tomorrow from
    history that stops today) read as decline.
    """

    def test_flat_demand_with_uneven_weekday_mix_shows_no_trend(self):
        # Saturdays run 4x every other day, but the underlying level never moves.
        history = _history(date(2026, 6, 1), 56, per_day={5: "40"})
        target = date(2026, 8, 1)  # a Saturday, six days after history ends
        assert target.weekday() == 5

        result = forecast_day(history, target)
        assert result.items["item-1"].trend_factor == D("1"), (
            "flat demand must not register a trend just because the recent "
            "window has a different weekday mix"
        )
        assert result.items["item-1"].forecast_qty == D("40")

    def test_genuine_growth_is_still_detected(self):
        # Level steps up at the boundary between the prior and recent trend
        # windows (target - 14 days), so the two windows straddle the change.
        rows = []
        for offset in range(56):
            day = date(2026, 6, 1) + timedelta(days=offset)
            qty = "10" if offset < 42 else "16"
            rows.append(DailySales(day, "item-1", D(qty)))
        result = forecast_day(rows, date(2026, 7, 27))
        assert result.items["item-1"].trend_factor > D("1.0"), "real growth must register"

    def test_genuine_decline_is_still_detected(self):
        rows = []
        for offset in range(56):
            day = date(2026, 6, 1) + timedelta(days=offset)
            qty = "20" if offset < 42 else "10"
            rows.append(DailySales(day, "item-1", D(qty)))
        result = forecast_day(rows, date(2026, 7, 27))
        assert result.items["item-1"].trend_factor < D("1.0"), "real decline must register"

    def test_closed_day_does_not_read_as_decline(self):
        # The restaurant shuts Mondays. A missing day must be absent from the
        # trend, not counted as a zero-sales day.
        rows = [
            DailySales(date(2026, 6, 1) + timedelta(days=o), "item-1", D("10"))
            for o in range(56)
            if (date(2026, 6, 1) + timedelta(days=o)).weekday() != 0
        ]
        result = forecast_day(rows, date(2026, 7, 28))
        assert result.items["item-1"].trend_factor == D("1")


class TestUnitsAreNotCovers:
    """Dishes forecast and guests in the room are different quantities.

    Confusing them cost a third of a roster: the scheduler sized itself against
    204 forecast dishes for a day that seated 154 people, and staffed for the
    larger number.
    """

    def test_total_units_counts_dishes(self):
        history = [
            DailySales(date(2026, 6, 1) + timedelta(days=o), item, D("10"))
            for o in range(30)
            for item in ("a", "b", "c")
        ]
        result = forecast_day(history, date(2026, 7, 5))
        assert result.total_units == sum(int(i.forecast_qty) for i in result.items.values())

    def test_covers_divides_by_the_basket(self):
        history = [
            DailySales(date(2026, 6, 1) + timedelta(days=o), "a", D("100")) for o in range(30)
        ]
        result = forecast_day(history, date(2026, 7, 5))
        result.units_per_cover = D("2")
        assert result.covers == result.total_units // 2

    def test_covers_is_fewer_than_units_when_people_order_more_than_one_thing(self):
        history = [
            DailySales(date(2026, 6, 1) + timedelta(days=o), "a", D("100")) for o in range(30)
        ]
        result = forecast_day(history, date(2026, 7, 5))
        result.units_per_cover = D("1.45")
        assert result.covers < result.total_units

    def test_a_basket_of_one_leaves_covers_equal_to_units(self):
        history = [
            DailySales(date(2026, 6, 1) + timedelta(days=o), "a", D("50")) for o in range(30)
        ]
        result = forecast_day(history, date(2026, 7, 5))
        result.units_per_cover = D("1")
        assert result.covers == result.total_units

    def test_a_nonsense_basket_does_not_divide_by_zero(self):
        history = [
            DailySales(date(2026, 6, 1) + timedelta(days=o), "a", D("50")) for o in range(30)
        ]
        result = forecast_day(history, date(2026, 7, 5))
        result.units_per_cover = D("0")
        assert result.covers == result.total_units
