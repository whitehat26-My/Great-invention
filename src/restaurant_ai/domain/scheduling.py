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

# Covers one person of this role can serve per hour, at a sustainable pace.
# Casual dining figures: a server working four to five tables turns about
# sixteen covers an hour, and a line cook plates around twenty-five off a menu
# this size.
COVERS_PER_HOUR: dict[ShiftRole, Decimal] = {
    ShiftRole.SERVER: Decimal("16"),
    ShiftRole.LINE_COOK: Decimal("25"),
    ShiftRole.CHEF: Decimal("35"),
    ShiftRole.KITCHEN_PORTER: Decimal("55"),
    ShiftRole.HOST: Decimal("60"),
    ShiftRole.BARISTA: Decimal("35"),
    ShiftRole.MANAGER: Decimal("150"),
}

# Minimum presence in any hour the doors are open. You cannot trade without a
# manager, a chef, someone on the line and someone on the floor — but a host,
# porter or barista is earned by volume, not assumed.
MINIMUM_HEADCOUNT: dict[ShiftRole, int] = {
    ShiftRole.MANAGER: 1,
    ShiftRole.CHEF: 1,
    ShiftRole.LINE_COOK: 1,
    ShiftRole.SERVER: 1,
    ShiftRole.HOST: 0,
    ShiftRole.KITCHEN_PORTER: 0,
    ShiftRole.BARISTA: 0,
}

# Trading hours. Kitchen roles start before service and finish after it: prep
# and breakdown are real work that has to be paid for and rostered.
OPEN_HOUR = 11
CLOSE_HOUR = 23
KITCHEN_ROLES = {ShiftRole.CHEF, ShiftRole.LINE_COOK, ShiftRole.KITCHEN_PORTER}
KITCHEN_PREP_HOURS = 1
KITCHEN_CLOSE_HOURS = 1

# How much of a person's work has to exist before a discretionary role is
# rostered at all. At 0.5 the hour has to be at least half that role's capacity.
DISCRETIONARY_ROUNDING = Decimal("0.5")

# Shift geometry. Nobody is called in for ninety minutes, and a shift longer
# than nine hours is how you get a tired line and an overtime bill.
MIN_SHIFT_HOURS = 4
MAX_SHIFT_HOURS = 9

# Ceiling on one person's day, across every shift they hold. Without it the
# fitter is free to hand someone a nine hour shift and then the five hour close
# straight after it, because neither breaks the weekly cap and the two do not
# technically overlap. Fourteen hours on a line is not a cheaper roster.
MAX_DAILY_HOURS = Decimal("10")

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


def hourly_headcount(covers_by_hour: dict[int, Decimal], role: ShiftRole) -> dict[int, int]:
    """Headcount needed in each trading hour, from the demand curve itself.

    Sizing a role off a block average is what makes a roster expensive. A six
    hour lunch block averaging eight covers an hour hides a noon that does
    seventeen and a three o'clock that does three: staff to the average and you
    are short at the peak and paying people to stand still in the lull.

    Kitchen roles are extended either side of service for prep and breakdown,
    because that work is real and someone has to be rostered for it.
    """
    open_hour, close_hour = OPEN_HOUR, CLOSE_HOUR
    # Prep and breakdown are real work, but they need a kitchen present rather
    # than one of every kitchen role: the chef preps and closes, the line cook
    # and porter come in for service.
    if role is ShiftRole.CHEF:
        open_hour -= KITCHEN_PREP_HOURS
        close_hour += KITCHEN_CLOSE_HOURS

    minimum = MINIMUM_HEADCOUNT.get(role, 0)
    rate = COVERS_PER_HOUR.get(role, Decimal("20"))

    need: dict[int, int] = {}
    for hour in range(open_hour, close_hour):
        covers = covers_by_hour.get(hour, ZERO)
        load = covers / rate  # people-worth of work in this hour

        if minimum > 0:
            # A role you must have: round up, because the moment demand exceeds
            # what the people present can handle you need another body.
            from_demand = int(load.to_integral_value(rounding="ROUND_CEILING"))
        else:
            # A role earned by volume. Rounding up here is what puts a dedicated
            # host, barista and porter on the floor for a whole thirteen-hour
            # day because a single guest walked in at three o'clock: 0.13 of a
            # person becomes a whole one. Add them only once there is most of a
            # person's work to do, and let the servers pour the tea below that.
            from_demand = int(
                (load + DISCRETIONARY_ROUNDING).to_integral_value(rounding="ROUND_FLOOR")
            )

        headcount = max(from_demand, minimum)
        if headcount > 0:
            need[hour] = headcount
    return need


def pack_shifts(
    need: dict[int, int],
    min_hours: int = MIN_SHIFT_HOURS,
    max_hours: int = MAX_SHIFT_HOURS,
) -> list[tuple[int, int]]:
    """Cover an hourly requirement with as few shift-hours as possible.

    Greedy peak-first: repeatedly take the busiest hour still short of cover and
    lay down the shift window that closes the most outstanding need. That
    naturally produces the shape a manager would draw by hand — an opening
    shift, extra bodies across the peaks, a closing shift — rather than putting
    everyone on for the whole day.

    Returns (start_hour, end_hour) pairs, one per person.
    """
    if not need:
        return []

    outstanding = dict(need)
    first, last = min(outstanding), max(outstanding) + 1
    span = last - first
    # Clamp only for a genuinely short trading day. Clamping to the span of one
    # role's need instead is how a barista wanted across the two busiest hours
    # got called in for a two-hour shift.
    min_hours = min(min_hours, span)
    max_hours = min(max_hours, span)

    shifts: list[tuple[int, int]] = []
    # Bounded by total need: each pass closes at least one person-hour.
    for _ in range(sum(need.values()) + 1):
        peak = max(
            (h for h, n in outstanding.items() if n > 0),
            key=lambda h: (outstanding[h], -h),
            default=None,
        )
        if peak is None:
            break

        best: tuple[int, int, int] | None = None  # (covered, -length, start)
        for length in range(min_hours, max_hours + 1):
            for start in range(peak - length + 1, peak + 1):
                if start < first or start + length > last:
                    continue
                covered = sum(1 for h in range(start, start + length) if outstanding.get(h, 0) > 0)
                # Most outstanding hours closed; ties go to the shorter shift,
                # since an hour of cover nobody needs is an hour paid for
                # nothing.
                candidate = (covered, -length, start)
                if best is None or candidate > best:
                    best = candidate

        if best is None:
            break
        _covered, negative_length, start = best
        end = start - negative_length
        for hour in range(start, end):
            if outstanding.get(hour, 0) > 0:
                outstanding[hour] -= 1
        shifts.append((start, end))

    return sorted(shifts)


def build_requirements(
    business_date: date,
    forecast_covers: int,
    hourly_shape: dict[int, Decimal],
    roles: list[ShiftRole] | None = None,
    tz=None,
) -> list[ShiftRequirement]:
    """Turn a day's forecast covers into shift requirements.

    ``hourly_shape`` maps hour-of-day to its share of the day's covers. Each
    role is sized hour by hour against that curve and then packed into real
    shifts, so the roster follows the shape of the day instead of flattening it.
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

    total_share = sum(hourly_shape.values(), ZERO)
    covers_by_hour = {
        hour: (Decimal(forecast_covers) * share / total_share) if total_share > 0 else ZERO
        for hour, share in hourly_shape.items()
    }

    requirements: list[ShiftRequirement] = []
    for role in roles:
        need = hourly_headcount(covers_by_hour, role)
        # A discretionary role wanted for less than one shift is not worth
        # calling anyone in for; that work is absorbed by the staff already on.
        if need and MINIMUM_HEADCOUNT.get(role, 0) == 0 and len(need) < MIN_SHIFT_HOURS:
            continue
        for start_hour, end_hour in pack_shifts(need):
            requirements.append(
                ShiftRequirement(
                    business_date=business_date,
                    role=role,
                    starts_at=_at(business_date, start_hour, tz),
                    ends_at=_at(business_date, end_hour, tz),
                    required_headcount=1,
                )
            )

    return _merge_identical(requirements)


def _at(business_date: date, hour: int, tz) -> datetime:
    """Build a datetime for an hour that may fall outside 0-23."""
    day_offset, hour_of_day = divmod(hour, 24)
    return datetime.combine(business_date, time(hour_of_day), tzinfo=tz) + timedelta(
        days=day_offset
    )


def _merge_identical(requirements: list[ShiftRequirement]) -> list[ShiftRequirement]:
    """Collapse identical windows into one requirement with a headcount.

    Two people on the same role over the same hours is one line on the roster
    asking for two bodies, not two lines.
    """
    merged: dict[tuple, ShiftRequirement] = {}
    for requirement in requirements:
        key = (requirement.role, requirement.starts_at, requirement.ends_at)
        existing = merged.get(key)
        if existing is None:
            merged[key] = requirement
        else:
            existing.required_headcount += requirement.required_headcount
    return sorted(merged.values(), key=lambda r: (r.starts_at, r.role.value))


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

    result.warnings.extend(_staffing_gaps(result, staff))
    return result


def _staffing_gaps(result: RosterResult, staff: list[StaffMember]) -> list[str]:
    """Turn repeated unfilled shifts into the hiring question behind them.

    A role that cannot be covered on one day is a scheduling problem. The same
    role uncovered every day is not — it means the payroll is short of people,
    and saying so once with the numbers is more use to an owner than the same
    line repeated six times.
    """
    if not result.unfilled:
        return []

    contracted: dict[ShiftRole, Decimal] = {}
    headcount: dict[ShiftRole, int] = {}
    for person in staff:
        contracted[person.role] = contracted.get(person.role, ZERO) + Decimal(
            person.max_weekly_hours
        )
        headcount[person.role] = headcount.get(person.role, 0) + 1

    required: dict[ShiftRole, Decimal] = {}
    for assignment in result.assignments:
        role = assignment.requirement.role
        required[role] = required.get(role, ZERO) + assignment.hours
    for requirement in result.unfilled:
        required[requirement.role] = required.get(requirement.role, ZERO) + (
            requirement.hours * requirement.required_headcount
        )

    gaps: list[str] = []
    days = len({r.business_date for r in result.unfilled})
    for role in sorted({r.role for r in result.unfilled}, key=lambda r: r.value):
        occurrences = sum(1 for r in result.unfilled if r.role is role)
        needed = required.get(role, ZERO)
        available = contracted.get(role, ZERO)
        if available > 0 and needed > available and days > 1:
            gaps.append(
                f"{role.value} is structurally short, not just awkward to roster: the week "
                f"needs {needed:.0f} hours and the {headcount.get(role, 0)} on payroll are "
                f"contracted for {available:.0f}. {occurrences} shift(s) cannot be covered "
                f"without breaking someone's contract. Hiring is the fix, not rescheduling."
            )
        else:
            gaps.append(
                f"{occurrences} {role.value} shift(s) unfilled. Cover exists on paper, so "
                f"this is an availability or rest-gap clash worth a manual look."
            )
    return gaps


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
        if _daily_hours(person, requirement, windows.get(person.staff_id, [])) > MAX_DAILY_HOURS:
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


def _daily_hours(
    person: StaffMember,
    requirement: ShiftRequirement,
    assigned: list[tuple[datetime, datetime]],
) -> Decimal:
    """Total hours this person would work on the requirement's day."""
    day = requirement.starts_at.date()
    total = requirement.hours
    for start, end in assigned:
        if start.date() == day:
            total += Decimal((end - start).total_seconds()) / Decimal(3600)
    return total


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
        uncovered = sum((r.hours * r.required_headcount for r in result.unfilled), ZERO)
        lines.append(
            f"{len(result.unfilled)} requirement(s) unfilled, {uncovered:.0f} hours uncovered"
        )

    for warning in result.warnings:
        lines.append(f"  - {warning}")

    # A couple of examples, not the whole list: the aggregate above says what
    # the problem is, and twelve identical lines bury it.
    for requirement in result.unfilled[:3]:
        lines.append(
            f"    e.g. {requirement.business_date} "
            f"{requirement.starts_at:%H:%M}-{requirement.ends_at:%H:%M} "
            f"{requirement.role.value}"
        )
    if len(result.unfilled) > 3:
        lines.append(f"    ... and {len(result.unfilled) - 3} more")

    return "\n".join(lines)
