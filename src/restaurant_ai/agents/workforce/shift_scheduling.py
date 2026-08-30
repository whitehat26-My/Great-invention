"""Shift Scheduling Agent.

Builds the weekly roster by converting the demand forecast into labour hours,
then fitting people to shifts at the lowest cost that satisfies every hard
constraint.

The hard constraints come from employment law and the staff contracts:
availability, contracted weekly hours, and minimum rest between shifts. A roster
that breaks one is not cheaper, it is invalid, and the fitter will leave a shift
unfilled rather than break one. Overtime is a cost to minimise; the contract cap
is a line not to cross.
"""

from __future__ import annotations

from datetime import date, time, timedelta
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, Field
from sqlalchemy import delete, select

from restaurant_ai import clock
from restaurant_ai.agents.common import sales_history, units_per_cover
from restaurant_ai.db.models import (
    Availability,
    Shift,
    ShiftAssignment,
    Staff,
)
from restaurant_ai.domain.forecasting import forecast_day, weekday_profile
from restaurant_ai.domain.scheduling import (
    StaffMember,
    build_requirements,
    build_roster,
    summarise_roster,
)
from restaurant_ai.events import Event, Topic, publish
from restaurant_ai.kernel.registry import register
from restaurant_ai.kernel.spec import AgentSpec, ToolContext, ToolSpec

ZERO = Decimal("0")

# Share of covers by hour, used to shape lunch against dinner.
DEFAULT_SHAPE = {
    11: Decimal("0.05"),
    12: Decimal("0.11"),
    13: Decimal("0.09"),
    14: Decimal("0.04"),
    15: Decimal("0.02"),
    16: Decimal("0.03"),
    17: Decimal("0.05"),
    18: Decimal("0.10"),
    19: Decimal("0.15"),
    20: Decimal("0.16"),
    21: Decimal("0.11"),
    22: Decimal("0.09"),
}


class RosterArgs(BaseModel):
    week_starting: str | None = Field(None, description="ISO date of the Monday; defaults to next.")
    days: int = Field(7, description="How many days to roster.")


def _staff_members(session) -> list[StaffMember]:
    people = list(session.execute(select(Staff).where(Staff.is_active)).scalars())
    availability: dict[str, dict[int, list[tuple[time, time]]]] = {}
    for row in session.execute(select(Availability)).scalars():
        availability.setdefault(row.staff_id, {}).setdefault(row.weekday, []).append(
            (row.start_time, row.end_time)
        )
    return [
        StaffMember(
            staff_id=p.id,
            name=p.name,
            role=p.role,
            hourly_rate=p.hourly_rate,
            max_weekly_hours=p.max_weekly_hours,
            min_rest_hours=p.min_rest_hours,
            availability=availability.get(p.id, {}),
        )
        for p in people
    ]


class HireArgs(BaseModel):
    name: str = Field(..., description="The person's name, as the owner said it.")
    role: str = Field(
        ...,
        description="chef, line_cook, kitchen_porter, server, host, barista or manager.",
    )


class AvailabilityArgs(BaseModel):
    who: str = Field(..., description="The person's name, or enough of it to identify them.")
    days: str = Field(
        ...,
        description=(
            "Which days they CAN work, as numbers 0-6 where 0 is Monday. "
            "'0,1,2,3,4' is Monday to Friday. Empty means they cannot work at all."
        ),
    )
    start: str = Field("10:00", description="Earliest they can start, as HH:MM.")
    end: str = Field("23:59", description="Latest they can work until, as HH:MM.")


def _find_person(session, who: str) -> tuple[Any, list[str]]:
    """The one person this names, or everyone it could have been.

    Never a closest match. Changing the wrong person's availability rosters
    somebody who cannot come and leaves somebody who can sitting at home, and
    neither shows up as an error — it shows up on a Friday night, short-staffed.
    """
    from sqlalchemy import select as _select

    needle = " ".join((who or "").lower().split())
    if not needle:
        return None, []
    everyone = list(session.execute(_select(Staff).where(Staff.is_active)).scalars())
    exact = [p for p in everyone if p.name.lower() == needle]
    if len(exact) == 1:
        return exact[0], []
    close = [p for p in everyone if needle in p.name.lower()]
    if len(close) == 1:
        return close[0], []
    return None, sorted(p.name for p in close or everyone)[:8]


def hire(context: ToolContext, name: str, role: str) -> dict[str, Any]:
    """Put someone on the books from a message, with nothing but a name and a job."""
    from restaurant_ai.db.models import ShiftRole

    wanted = role.lower().replace(" ", "_").replace("-", "_")
    valid = {r.value for r in ShiftRole}
    if wanted not in valid:
        return {"hired": False, "note": f"'{role}' is not a role here.", "roles": sorted(valid)}

    session = context.session
    existing, _ = _find_person(session, name)
    if existing is not None:
        return {"hired": False, "note": f"{existing.name} is already on the books."}

    taken = len(list(session.execute(select(Staff)).scalars()))
    person = Staff(
        employee_code=f"EMP-{taken + 1:03d}",
        name=name.strip()[:160],
        role=wanted,
        # Unknown, not free. The owner can say the wage later; a number invented
        # here would quietly become the labour cost in Camelia's report.
        hourly_rate=Decimal("0"),
        max_weekly_hours=48,
        min_rest_hours=11,
        is_active=True,
    )
    session.add(person)
    session.flush()
    # Available every day until told otherwise, so they can be rostered at once.
    for weekday in range(7):
        person.availability.append(
            Availability(weekday=weekday, start_time=time(10, 0), end_time=time(23, 59))
        )
    return {
        "hired": True,
        "name": person.name,
        "role": wanted,
        "code": person.employee_code,
        "note": "Available every day until you tell me otherwise. Wage not set.",
    }


def set_availability(
    context: ToolContext, who: str, days: str, start: str = "10:00", end: str = "23:59"
) -> dict[str, Any]:
    """Change which days and hours someone can work."""
    session = context.session
    person, candidates = _find_person(session, who)
    if person is None:
        return {
            "changed": False,
            "note": f"I could not tell who '{who}' is." if candidates else "Nobody on the books.",
            "candidates": candidates,
        }

    wanted: list[int] = []
    for part in (days or "").split(","):
        part = part.strip()
        if not part:
            continue
        if not part.isdigit() or not 0 <= int(part) <= 6:
            return {"changed": False, "note": f"'{part}' is not a day. Use 0-6, 0 = Monday."}
        wanted.append(int(part))

    try:
        opens, closes = _clock_of(start), _clock_of(end)
    except ValueError:
        return {"changed": False, "note": f"'{start}' and '{end}' should look like 09:00."}

    # A bulk delete goes round the session, so the rows it removed are still
    # in this object's collection and SQLAlchemy re-inserts them on the next
    # flush. Expiring the collection makes it re-read what is actually there.
    session.execute(delete(Availability).where(Availability.staff_id == person.id))
    session.expire(person, ["availability"])
    session.flush()
    for weekday in sorted(set(wanted)):
        person.availability.append(Availability(weekday=weekday, start_time=opens, end_time=closes))
    names = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    return {
        "changed": True,
        "who": person.name,
        "days": [names[d] for d in sorted(set(wanted))] or "none — cannot work at all",
        "hours": f"{start}-{end}",
    }


def _clock_of(text: str) -> time:
    hour, _, minute = str(text).strip().partition(":")
    return time(int(hour), int(minute or 0))


def perceive(context: ToolContext) -> dict[str, Any]:
    session = context.session
    people = list(session.execute(select(Staff).where(Staff.is_active)).scalars())
    by_role: dict[str, int] = {}
    for person in people:
        by_role[person.role.value] = by_role.get(person.role.value, 0) + 1

    next_monday = context.business_date + timedelta(
        days=(7 - context.business_date.weekday()) % 7 or 7
    )
    existing = session.execute(
        select(Shift).where(
            Shift.business_date >= next_monday,
            Shift.business_date < next_monday + timedelta(days=7),
        )
    ).scalars()

    return {
        "active_staff": len(people),
        "by_role": by_role,
        "week_starting": next_monday.isoformat(),
        "shifts_already_rostered": len(list(existing)),
    }


def build_week(
    context: ToolContext, week_starting: str | None = None, days: int = 7
) -> dict[str, Any]:
    """Forecast demand per day, convert to labour, and fit the roster."""
    session = context.session

    if week_starting:
        start = date.fromisoformat(week_starting)
    else:
        start = context.business_date + timedelta(
            days=(7 - context.business_date.weekday()) % 7 or 7
        )

    history = sales_history(session, 56, until=context.business_date)
    if not history:
        return {"shifts": 0, "note": "No sales history; cannot size the roster."}

    profile = weekday_profile(history)
    basket = units_per_cover(session, until=context.business_date)
    people = _staff_members(session)
    if not people:
        return {"shifts": 0, "note": "No active staff to roster."}

    all_requirements = []
    daily: list[dict[str, Any]] = []

    for offset in range(days):
        day = start + timedelta(days=offset)
        if day.weekday() == 0:  # closed Mondays
            continue

        forecast = forecast_day(history, day)
        forecast.units_per_cover = basket
        # Covers, not units. The forecaster predicts dishes; the roster is sized
        # by how many people are in the room, and at a 1.4-dish basket those
        # differ by a third.
        covers = forecast.covers
        if not covers:
            # No usable forecast: fall back to the recent daily average, still
            # converted from dishes to guests.
            recent = history[-30:]
            mean_units = sum((r.quantity for r in recent), ZERO) / Decimal(max(len(recent), 1))
            covers = int(mean_units / (basket if basket > 0 else Decimal("1")))
        # Nudge by how this weekday usually trades.
        covers = int(Decimal(covers) * profile.get(day.weekday(), Decimal("1")))

        requirements = build_requirements(day, covers, DEFAULT_SHAPE, tz=clock.local_tz())
        all_requirements.extend(requirements)
        daily.append({"date": day.isoformat(), "weekday": day.strftime("%A"), "covers": covers})

    result = build_roster(all_requirements, people)

    # Replace any existing roster for the window rather than layering on it.
    end = start + timedelta(days=days)
    for shift in session.execute(
        select(Shift).where(Shift.business_date >= start, Shift.business_date < end)
    ).scalars():
        session.delete(shift)
    session.flush()

    shifts_by_key: dict[tuple, Shift] = {}
    for assignment in result.assignments:
        requirement = assignment.requirement
        key = (requirement.business_date, requirement.role, requirement.starts_at)
        existing = shifts_by_key.get(key)
        if existing is None:
            shift = Shift(
                business_date=requirement.business_date,
                role=requirement.role,
                starts_at=requirement.starts_at,
                ends_at=requirement.ends_at,
                required_headcount=requirement.required_headcount,
                run_id=context.run_id,
            )
            session.add(shift)
            session.flush()
            shifts_by_key[key] = shift
        else:
            shift = existing

        session.add(
            ShiftAssignment(
                shift_id=shift.id,
                staff_id=assignment.staff_id,
                estimated_cost=assignment.cost,
                is_confirmed=False,
            )
        )

    session.flush()
    forecast_revenue = ZERO  # filled by the daily report once actuals land
    publish(
        Event(
            Topic.ROSTER_PUBLISHED,
            {"week_starting": start.isoformat(), "shifts": len(shifts_by_key)},
            source_run_id=context.run_id,
        ),
        session=session,
    )

    return {
        "week_starting": start.isoformat(),
        "shifts": len(shifts_by_key),
        "assignments": len(result.assignments),
        "unfilled": len(result.unfilled),
        "total_hours": str(result.total_hours),
        "total_cost": str(result.total_cost),
        "overtime_shifts": sum(1 for a in result.assignments if a.is_overtime),
        "warnings": result.warnings,
        "days": daily,
        "summary": summarise_roster(result, forecast_revenue),
        "hours_by_staff": {
            next((p.name for p in people if p.staff_id == sid), sid): str(hours)
            for sid, hours in sorted(
                result.hours_by_staff.items(), key=lambda kv: kv[1], reverse=True
            )
        },
    }


def autonomous(context: ToolContext, perceived: dict[str, Any]) -> dict[str, Any]:
    # A caller can pin the window (the simulator rosters a single day); by
    # default this builds next week.
    args: dict[str, Any] = {"days": int(context.state.get("days", 7))}
    if context.state.get("week_starting"):
        args["week_starting"] = context.state["week_starting"]

    target = args.get("week_starting") or perceived.get("week_starting")
    return {
        "summary": (
            f"Building the roster from {target} across "
            f"{perceived.get('active_staff')} active staff."
        ),
        "results": {},
        "tool_calls": [{"name": "build_week", "args": args}],
    }


SHIFT_SCHEDULING_AGENT = register(
    AgentSpec(
        name="shift_scheduling",
        person="Henry",
        department="workforce",
        title="Shift Scheduling Agent",
        description=(
            "Builds weekly staff rosters by aligning labour hours with projected demand "
            "curves, preventing over- and understaffing."
        ),
        system_prompt=(
            "You are the Shift Scheduling Agent for a restaurant.\n\n"
            "You turn a demand forecast into a roster. Understaff a Saturday and service "
            "collapses; overstaff a Tuesday and the labour line eats the month's profit.\n\n"
            "Some constraints are not yours to trade away. A person can only work when they "
            "are available, cannot exceed their contracted hours, and must have their minimum "
            "rest between shifts. If that leaves a shift unfilled, leave it unfilled and say "
            "so - do not quietly break a contract to close a gap.\n\n"
            "Within what is legal, minimise cost: spread hours to keep people out of overtime, "
            "and match the shape of the day rather than rostering a flat line from open to "
            "close. Always report what you could not fill and why.\n\n"
            "WHO WORKS HERE\n"
            "The owner tells you in a message, in their own words: 'Ahmad cannot work "
            "Fridays', 'take on Kumar as a barista', 'Siti can only do mornings now'. Act on "
            "it — that is how a shift change actually reaches anyone in a place this size, "
            "and a change nobody records is one that gets forgotten by Wednesday.\n\n"
            "You are told a name and a job, and the rest is yours: assume someone can work "
            "every day the restaurant is open until you hear otherwise, and roster them "
            "accordingly. Never invent a wage. An hourly rate you made up becomes the labour "
            "cost in Camelia's report and nobody would know where it came from — if you need "
            "one, ask for it.\n\n"
            "If a name could be two people, ask which. Changing the wrong person's days "
            "rosters somebody who cannot come and leaves somebody who can at home, and "
            "neither shows up as an error — it shows up on a Friday night, short-staffed.\n\n"
            "HOW YOU WORK\n"
            "- `hire` — put someone on the books from a name and a role.\n"
            "- `set_availability` — change which days and hours someone can work.\n"
            "- `build_week` — the roster itself: forecast covers, convert to hours by role, "
            "fit against availability, hours caps and rest. A roster described but not built "
            "is a week the floor has no cover for.\n\n"
            "Do what was asked, then build the week if the change affects it. Being told "
            "somebody cannot work Friday is a reason to rebuild Friday.\n"
        ),
        model_tier="reasoning",
        tools=[
            ToolSpec(
                name="build_week",
                description=(
                    "Forecast each day's covers, convert to labour hours by role, and fit the "
                    "roster against availability, hours caps and rest requirements."
                ),
                fn=build_week,
                args_schema=RosterArgs,
            ),
            ToolSpec(
                name="hire",
                description=(
                    "Put someone on the books from a name and a role. Everything else is "
                    "assumed: available every day, no wage set."
                ),
                fn=hire,
                args_schema=HireArgs,
            ),
            ToolSpec(
                name="set_availability",
                description=(
                    "Change which days and hours someone can work. Days are 0-6 with 0 = "
                    "Monday; an empty list means they cannot work at all."
                ),
                fn=set_availability,
                args_schema=AvailabilityArgs,
            ),
        ],
        # Three tools, and the loop spends a turn on each call and each result.
        max_iterations=9,
        perceive=perceive,
        autonomous=autonomous,
    )
)
