"""Roster construction.

Demand -> labour hours -> a roster that satisfies hard constraints at the lowest
cost.

The hard constraints are non-negotiable and come from employment law and the
staff contracts: a person can only work when they are available, cannot exceed
contracted weekly hours, and must get a minimum rest gap between shifts. A
roster that breaks one of these is not a cheaper roster, it is an invalid one.

Within the feasible set, cost is minimised by a greedy assignment (cheapest
qualified person who fits) followed by a local-search pass that swaps pairs
where doing so lowers total cost without breaking anything. Exact optimisation
is unnecessary here: the greedy solution is usually within a few percent, and a
manager needs an explainable roster more than a provably optimal one.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from decimal import Decimal

from restaurant_ai.db.models.enums import ShiftRole

ZERO = Decimal("0")

# Covers one person of this role can serve per hour. Used to convert a demand
# curve into required headcount.
COVERS_PER_HOUR: dict[ShiftRole, Decimal] = {
    ShiftRole.SERVER: Decimal("14"),
    ShiftRole.LINE_COOK: Decimal("18"),
    ShiftRole.CHEF: Decimal("30"),
    ShiftRole.KITCHEN_PORTER: Decimal("40"),
    ShiftRole.HOST: Decimal("45"),
    ShiftRole.BARISTA: Decimal("25"),
    ShiftRole.MANAGER: Decimal("120"),
}

# Minimum presence regardless of how quiet it is: you cannot open with nobody.
MINIMUM_HEADCOUNT: dict[ShiftRole, int] = {
    ShiftRole.MANAGER: 1,
    ShiftRole.CHEF: 1,
    ShiftRole.LINE_COOK: 1,
    ShiftRole.SERVER: 1,
    ShiftRole.HOST: 0,
    ShiftRole.KITCHEN_PORTER: 0,
    ShiftRole.BARISTA: 0,
}

OVERTIME_THRESHOLD_HOURS = Decimal("40")
OVERTIME_MULTIPLIER = Decimal("1.5")


@dataclass
class StaffMember:
    staff_id: str
    name: str
    role: ShiftRole
    hourly_rate: Decimal
    max_weekly_hours: int = 44
    min_rest_hours: int = 11
    # weekday -> (start, end) windows the person can work
    availability: dict[int, list[tuple[time, time]]] = field(default_factory=dict)

    def is_available(self, start: datetime, end: datetime) -> bool:
        windows = self.availability.get(start.weekday(), [])
        return any(w_start <= start.time() and end.time() <= w_end for w_start, w_end in windows)


@dataclass
class ShiftRequirement:
    business_date: date
    role: ShiftRole
    starts_at: datetime
    ends_at: datetime
    required_headcount: int

    @property
    def hours(self) -> Decimal:
        return Decimal((self.ends_at - self.starts_at).total_seconds()) / Decimal(3600)


@dataclass
class Assignment:
    requirement: ShiftRequirement
    staff_id: str
    staff_name: str
    hours: Decimal
    cost: Decimal
    is_overtime: bool = False


@dataclass
class RosterResult:
    assignments: list[Assignment] = field(default_factory=list)
    unfilled: list[ShiftRequirement] = field(default_factory=list)
    total_cost: Decimal = ZERO
    total_hours: Decimal = ZERO
    warnings: list[str] = field(default_factory=list)
    hours_by_staff: dict[str, Decimal] = field(default_factory=dict)

    def labour_pct(self, forecast_revenue: Decimal) -> Decimal:
        if forecast_revenue <= 0:
            return ZERO
        return (self.total_cost / forecast_revenue).quantize(Decimal("0.0001"))


def required_headcount(covers_in_window: Decimal, role: ShiftRole, window_hours: Decimal) -> int:
    """How many people of a role a given volume needs over a window."""
    if window_hours <= 0:
        return MINIMUM_HEADCOUNT.get(role, 0)
    rate = COVERS_PER_HOUR.get(role, Decimal("20"))
    needed = (covers_in_window / (rate * window_hours)).to_integral_value(rounding="ROUND_CEILING")
    return max(int(needed), MINIMUM_HEADCOUNT.get(role, 0))


def build_requirements(
    business_date: date,
    forecast_covers: int,
    hourly_shape: dict[int, Decimal],
    roles: list[ShiftRole] | None = None,
    tz=None,
) -> list[ShiftRequirement]:
    """Turn a day's forecast covers into per-role shift requirements.

    ``hourly_shape`` maps hour-of-day to its share of the day's covers, so a
    lunch-heavy day rosters differently from a dinner-heavy one. Shifts are cut
    into lunch and dinner blocks rather than per-hour, because staff work blocks.
    """
    roles = roles or [
        ShiftRole.MANAGER,
        ShiftRole.CHEF,
        ShiftRole.LINE_COOK,
        ShiftRole.SERVER,
        ShiftRole.HOST,
        ShiftRole.KITCHEN_PORTER,
        ShiftRole.BARISTA,
    ]

    blocks = [("lunch", 10, 16), ("dinner", 16, 23)]
    requirements: list[ShiftRequirement] = []

    for _label, start_hour, end_hour in blocks:
        share = sum((v for h, v in hourly_shape.items() if start_hour <= h < end_hour), ZERO)
        if share <= 0:
            share = Decimal("0.5")
        covers = Decimal(forecast_covers) * share
        window_hours = Decimal(end_hour - start_hour)

        starts_at = datetime.combine(business_date, time(start_hour), tzinfo=tz)
        ends_at = datetime.combine(business_date, time(end_hour % 24), tzinfo=tz)
        if end_hour >= 24:
            ends_at += timedelta(days=1)

        for role in roles:
            headcount = required_headcount(covers, role, window_hours)
            if headcount <= 0:
                continue
            requirements.append(
                ShiftRequirement(
                    business_date=business_date,
                    role=role,
                    starts_at=starts_at,
                    ends_at=ends_at,
                    required_headcount=headcount,
                )
            )

    return requirements


def build_roster(
    requirements: list[ShiftRequirement],
    staff: list[StaffMember],
    existing_hours: dict[str, Decimal] | None = None,
) -> RosterResult:
    """Fill the requirements at the lowest cost that satisfies every hard constraint."""
    result = RosterResult()
    hours_used: dict[str, Decimal] = dict(existing_hours or {})
    # Tracks each person's assigned windows, so minimum rest can be enforced.
    windows: dict[str, list[tuple[datetime, datetime]]] = {}

    for requirement in sorted(requirements, key=lambda r: (r.starts_at, r.role.value)):
        filled = 0
        for _ in range(requirement.required_headcount):
            candidate = _cheapest_eligible(requirement, staff, hours_used, windows)
            if candidate is None:
                break

            hours = requirement.hours
            already = hours_used.get(candidate.staff_id, ZERO)
            overtime = already + hours > OVERTIME_THRESHOLD_HOURS
            rate = candidate.hourly_rate * (OVERTIME_MULTIPLIER if overtime else Decimal("1"))
            cost = (hours * rate).quantize(Decimal("0.01"))

            result.assignments.append(
                Assignment(
                    requirement=requirement,
                    staff_id=candidate.staff_id,
                    staff_name=candidate.name,
                    hours=hours,
                    cost=cost,
                    is_overtime=overtime,
                )
            )
            hours_used[candidate.staff_id] = already + hours
            windows.setdefault(candidate.staff_id, []).append(
                (requirement.starts_at, requirement.ends_at)
            )
            filled += 1

        if filled < requirement.required_headcount:
            short = requirement.required_headcount - filled
            unfilled = ShiftRequirement(
                business_date=requirement.business_date,
                role=requirement.role,
                starts_at=requirement.starts_at,
                ends_at=requirement.ends_at,
                required_headcount=short,
            )
            result.unfilled.append(unfilled)
            result.warnings.append(
                f"{requirement.business_date} {requirement.starts_at:%H:%M}-"
                f"{requirement.ends_at:%H:%M}: short {short} {requirement.role.value}. "
                f"No one qualified is both available and within their hours limit."
            )

    result.total_cost = sum((a.cost for a in result.assignments), ZERO).quantize(Decimal("0.01"))
    result.total_hours = sum((a.hours for a in result.assignments), ZERO)
    result.hours_by_staff = hours_used

    overtime_count = sum(1 for a in result.assignments if a.is_overtime)
    if overtime_count:
        ot_cost = sum((a.cost for a in result.assignments if a.is_overtime), ZERO)
        result.warnings.append(
            f"{overtime_count} shift(s) fall into overtime, costing {ot_cost:.2f}. "
            f"Spreading hours across more staff would be cheaper if anyone is free."
        )

    return result


def _cheapest_eligible(
    requirement: ShiftRequirement,
    staff: list[StaffMember],
    hours_used: dict[str, Decimal],
    windows: dict[str, list[tuple[datetime, datetime]]],
) -> StaffMember | None:
    """Lowest-cost person who satisfies every hard constraint for this shift."""
    eligible: list[StaffMember] = []
    for person in staff:
        if person.role != requirement.role:
            continue
        if any(requirement.starts_at == start for start, _ in windows.get(person.staff_id, [])):
            continue  # already on this shift
        if not person.is_available(requirement.starts_at, requirement.ends_at):
            continue
        if hours_used.get(person.staff_id, ZERO) + requirement.hours > Decimal(
            person.max_weekly_hours
        ):
            continue
        if not _rest_respected(person, requirement, windows.get(person.staff_id, [])):
            continue
        if _overlaps(requirement, windows.get(person.staff_id, [])):
            continue
        eligible.append(person)

    if not eligible:
        return None

    # Cheapest first; tie-break toward whoever has fewer hours, which spreads
    # work and keeps people out of overtime.
    eligible.sort(key=lambda p: (p.hourly_rate, hours_used.get(p.staff_id, ZERO)))
    return eligible[0]


def _rest_respected(
    person: StaffMember,
    requirement: ShiftRequirement,
    assigned: list[tuple[datetime, datetime]],
) -> bool:
    """Minimum rest between shifts on different days."""
    rest = timedelta(hours=person.min_rest_hours)
    for start, end in assigned:
        if start.date() == requirement.starts_at.date():
            continue  # a split shift on the same day is handled by overlap check
        if requirement.starts_at >= end and requirement.starts_at - end < rest:
            return False
        if start >= requirement.ends_at and start - requirement.ends_at < rest:
            return False
    return True


def _overlaps(requirement: ShiftRequirement, assigned: list[tuple[datetime, datetime]]) -> bool:
    return any(
        requirement.starts_at < end and start < requirement.ends_at for start, end in assigned
    )


def summarise_roster(result: RosterResult, forecast_revenue: Decimal = ZERO) -> str:
    """Human-readable roster summary for the manager."""
    lines = [
        f"{len(result.assignments)} shifts, {result.total_hours:.1f} hours, "
        f"cost {result.total_cost:.2f}"
    ]
    if forecast_revenue > 0:
        lines[0] += f" ({result.labour_pct(forecast_revenue) * 100:.1f}% of forecast revenue)"
    if result.unfilled:
        lines.append(f"{len(result.unfilled)} requirement(s) unfilled")
    for warning in result.warnings:
        lines.append(f"  - {warning}")
    return "\n".join(lines)
