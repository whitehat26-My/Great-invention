"""Demand forecasting.

The model is a multiplicative seasonal-naive: a per-item baseline, scaled by the
day-of-week factor, a recent-trend factor and an optional event multiplier. It is
deliberately simple and explainable — a chef has to be able to look at tomorrow's
prep list and understand why it says what it says — but it recovers the real
structure of restaurant demand, which is dominated by day-of-week.

Accuracy is scored the next day against actual sales, and the resulting bias is
fed back so persistent over- or under-forecasting self-corrects.
"""

from __future__ import annotations

import statistics
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal

ZERO = Decimal("0")

# How many same-weekday observations to average for the baseline.
DEFAULT_SEASONS = 4
# Trend is the ratio of the last `TREND_WINDOW` days to the preceding window.
TREND_WINDOW = 14
# Clamps, so one freak day cannot blow up tomorrow's prep.
MIN_TREND, MAX_TREND = Decimal("0.75"), Decimal("1.35")
MIN_BIAS, MAX_BIAS = Decimal("0.80"), Decimal("1.25")


@dataclass
class DailySales:
    """One item's sales on one day — the unit of history the model consumes."""

    business_date: date
    menu_item_id: str
    quantity: Decimal


@dataclass
class ItemForecastResult:
    menu_item_id: str
    forecast_qty: Decimal
    baseline: Decimal
    weekday_factor: Decimal
    trend_factor: Decimal
    event_factor: Decimal
    bias_factor: Decimal
    observations: int
    method: str = "seasonal_naive"

    def explain(self) -> str:
        return (
            f"baseline {self.baseline} x trend {self.trend_factor} "
            f"x event {self.event_factor} x bias {self.bias_factor} "
            f"= {self.forecast_qty} (from {self.observations} same-weekday observations)"
        )


@dataclass
class ForecastResult:
    business_date: date
    items: dict[str, ItemForecastResult] = field(default_factory=dict)
    total_covers: int = 0

    @property
    def quantities(self) -> dict[str, Decimal]:
        return {k: v.forecast_qty for k, v in self.items.items()}


def forecast_day(
    history: list[DailySales],
    target_date: date,
    seasons: int = DEFAULT_SEASONS,
    event_factor: Decimal = Decimal("1"),
    bias: dict[str, Decimal] | None = None,
) -> ForecastResult:
    """Forecast per-item demand for ``target_date``.

    ``bias`` is an optional per-item correction learned from previous accuracy,
    where >1 means the model has been under-forecasting that item.
    """
    by_item: dict[str, list[DailySales]] = defaultdict(list)
    for row in history:
        if row.business_date < target_date:
            by_item[row.menu_item_id].append(row)

    trend = _trend_factor(history, target_date)
    result = ForecastResult(business_date=target_date)

    for menu_item_id, rows in by_item.items():
        rows.sort(key=lambda r: r.business_date)
        same_weekday = [r for r in rows if r.business_date.weekday() == target_date.weekday()]
        recent = same_weekday[-seasons:]

        if recent:
            baseline = _mean([r.quantity for r in recent])
            observations = len(recent)
            method = "seasonal_naive"
        else:
            # No same-weekday history yet: fall back to the overall recent mean.
            fallback = rows[-TREND_WINDOW:]
            if not fallback:
                continue
            baseline = _mean([r.quantity for r in fallback])
            observations = len(fallback)
            method = "recent_mean"

        item_bias = _clamp((bias or {}).get(menu_item_id, Decimal("1")), MIN_BIAS, MAX_BIAS)
        forecast = baseline * trend * event_factor * item_bias
        result.items[menu_item_id] = ItemForecastResult(
            menu_item_id=menu_item_id,
            forecast_qty=_round_up_portion(forecast),
            baseline=baseline.quantize(Decimal("0.01")),
            weekday_factor=Decimal("1"),
            trend_factor=trend,
            event_factor=event_factor,
            bias_factor=item_bias,
            observations=observations,
            method=method,
        )

    result.total_covers = int(sum(i.forecast_qty for i in result.items.values()))
    return result


def _trend_factor(history: list[DailySales], target_date: date) -> Decimal:
    """Underlying growth, measured with day-of-week seasonality removed.

    Trend and seasonality are confounded in raw daily volume. A window holding
    proportionally more weekend days reads as growth, and one ending mid-week
    reads as decline, when neither has happened — and because the restaurant is
    closed one day a week and history usually stops before the date being
    forecast, the two windows almost never have matching weekday composition.

    So each day is first divided by its own weekday's average level. What is left
    is that day's deviation from its normal, which is comparable across windows.
    The trend is then the ratio of the recent mean deviation to the prior one.
    """
    per_day: dict[date, Decimal] = defaultdict(Decimal)
    for row in history:
        per_day[row.business_date] += row.quantity
    if not per_day:
        return Decimal("1")

    weekday_totals: dict[int, list[Decimal]] = defaultdict(list)
    for day, total in per_day.items():
        weekday_totals[day.weekday()].append(total)
    weekday_mean = {wd: _mean(vals) for wd, vals in weekday_totals.items()}

    recent_start = target_date - timedelta(days=TREND_WINDOW)
    prior_start = target_date - timedelta(days=TREND_WINDOW * 2)

    recent: list[Decimal] = []
    prior: list[Decimal] = []
    for day, total in per_day.items():
        baseline = weekday_mean.get(day.weekday(), ZERO)
        if baseline <= 0:
            continue
        deviation = total / baseline
        if recent_start <= day < target_date:
            recent.append(deviation)
        elif prior_start <= day < recent_start:
            prior.append(deviation)

    if not recent or not prior:
        return Decimal("1")

    recent_mean = _mean(recent)
    prior_mean = _mean(prior)
    if prior_mean <= 0 or recent_mean <= 0:
        return Decimal("1")
    return _clamp((recent_mean / prior_mean).quantize(Decimal("0.0001")), MIN_TREND, MAX_TREND)


def weekday_profile(history: list[DailySales]) -> dict[int, Decimal]:
    """Average total volume per weekday, normalised so the mean weekday is 1.0.

    The scheduling agent uses this to shape labour hours across the week.
    """
    totals: dict[int, list[Decimal]] = defaultdict(list)
    per_day: dict[date, Decimal] = defaultdict(Decimal)
    for row in history:
        per_day[row.business_date] += row.quantity
    for day, total in per_day.items():
        totals[day.weekday()].append(total)

    means = {wd: _mean(vals) for wd, vals in totals.items() if vals}
    if not means:
        return {}
    overall = _mean(list(means.values()))
    if overall <= 0:
        return {}
    return {wd: (v / overall).quantize(Decimal("0.0001")) for wd, v in means.items()}


def intraday_profile(hourly: dict[int, Decimal]) -> dict[int, Decimal]:
    """Normalise an hour->volume map into shares of the day. Drives shift shaping."""
    total = sum(hourly.values(), ZERO)
    if total <= 0:
        return {}
    return {h: (v / total).quantize(Decimal("0.0001")) for h, v in hourly.items()}


def score_accuracy(forecast: dict[str, Decimal], actual: dict[str, Decimal]) -> dict[str, Decimal]:
    """Per-item absolute error, plus aggregate MAPE and bias under reserved keys.

    Bias is actual/forecast, so a value above 1 means demand was under-forecast
    and next run should prep more.
    """
    errors: dict[str, Decimal] = {}
    abs_pct_errors: list[Decimal] = []
    total_forecast = ZERO
    total_actual = ZERO

    for item_id in set(forecast) | set(actual):
        f = forecast.get(item_id, ZERO)
        a = actual.get(item_id, ZERO)
        errors[item_id] = abs(a - f)
        total_forecast += f
        total_actual += a
        if a > 0:
            abs_pct_errors.append((abs(a - f) / a).quantize(Decimal("0.0001")))

    errors["__mape__"] = (
        _mean(abs_pct_errors).quantize(Decimal("0.0001")) if abs_pct_errors else ZERO
    )
    errors["__bias__"] = (
        (total_actual / total_forecast).quantize(Decimal("0.0001"))
        if total_forecast > 0
        else Decimal("1")
    )
    return errors


def learn_bias(
    forecast: dict[str, Decimal], actual: dict[str, Decimal], damping: Decimal = Decimal("0.5")
) -> dict[str, Decimal]:
    """Per-item correction for the next run, damped so it converges rather than oscillates.

    A raw actual/forecast ratio would chase noise; halving the correction each
    day settles on the true level instead of ringing around it.
    """
    bias: dict[str, Decimal] = {}
    for item_id in set(forecast) | set(actual):
        f = forecast.get(item_id, ZERO)
        a = actual.get(item_id, ZERO)
        if f <= 0:
            continue
        raw = a / f
        damped = Decimal("1") + (raw - Decimal("1")) * damping
        bias[item_id] = _clamp(damped.quantize(Decimal("0.0001")), MIN_BIAS, MAX_BIAS)
    return bias


def _mean(values: list[Decimal]) -> Decimal:
    if not values:
        return ZERO
    return (sum(values, ZERO) / Decimal(len(values))).quantize(Decimal("0.0001"))


def stdev(values: list[Decimal]) -> Decimal:
    """Sample standard deviation; used for safety stock."""
    if len(values) < 2:
        return ZERO
    return Decimal(str(statistics.stdev([float(v) for v in values]))).quantize(Decimal("0.0001"))


def _clamp(value: Decimal, low: Decimal, high: Decimal) -> Decimal:
    return max(low, min(high, value))


def _round_up_portion(value: Decimal) -> Decimal:
    """Portions are whole. Round to nearest, but never below one for live demand."""
    if value <= 0:
        return ZERO
    rounded = value.quantize(Decimal("1"))
    return max(rounded, Decimal("1"))
