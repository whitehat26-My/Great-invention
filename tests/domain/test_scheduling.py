from datetime import date, datetime, time, timedelta
from decimal import Decimal

from restaurant_ai.db.models.enums import ShiftRole
from restaurant_ai.domain.scheduling import (
    ShiftRequirement,
    StaffMember,
    build_requirements,
    build_roster,
    required_headcount,
    summarise_roster,
)

D = Decimal
DAY = date(2026, 8, 27)  # a Thursday


def _staff(sid, role=ShiftRole.SERVER, rate="15.00", max_hours=44, always=True) -> StaffMember:
    availability = {wd: [(time(0, 0), time(23, 59))] for wd in range(7)} if always else {}
    return StaffMember(
        staff_id=sid,
        name=f"Staff {sid}",
        role=role,
        hourly_rate=D(rate),
        max_weekly_hours=max_hours,
        availability=availability,
    )


def _requirement(role=ShiftRole.SERVER, headcount=1, start=10, end=16, day=DAY):
    return ShiftRequirement(
        business_date=day,
        role=role,
        starts_at=datetime.combine(day, time(start)),
        ends_at=datetime.combine(day, time(end)),
        required_headcount=headcount,
    )


class TestRequiredHeadcount:
    def test_scales_with_volume(self):
        few = required_headcount(D("50"), ShiftRole.SERVER, D("6"))
        many = required_headcount(D("500"), ShiftRole.SERVER, D("6"))
        assert many > few

    def test_enforces_a_minimum_presence(self):
        # You cannot open with nobody, however quiet it is.
        assert required_headcount(D("0"), ShiftRole.MANAGER, D("6")) >= 1
        assert required_headcount(D("0"), ShiftRole.CHEF, D("6")) >= 1

    def test_optional_roles_can_be_zero(self):
        assert required_headcount(D("0"), ShiftRole.HOST, D("6")) == 0

    def test_rounds_up(self):
        # 1.1 people is 2 people.
        assert required_headcount(D("100"), ShiftRole.SERVER, D("6")) >= 2


class TestBuildRequirements:
    def test_creates_lunch_and_dinner_blocks(self):
        shape = {12: D("0.4"), 19: D("0.6")}
        reqs = build_requirements(DAY, 200, shape)
        starts = {r.starts_at.hour for r in reqs}
        assert starts == {10, 16}

    def test_busier_block_needs_more_people(self):
        shape = {12: D("0.2"), 19: D("0.8")}
        reqs = build_requirements(DAY, 400, shape, roles=[ShiftRole.SERVER])
        lunch = next(r for r in reqs if r.starts_at.hour == 10)
        dinner = next(r for r in reqs if r.starts_at.hour == 16)
        assert dinner.required_headcount > lunch.required_headcount


class TestBuildRoster:
    def test_fills_a_simple_requirement(self):
        result = build_roster([_requirement()], [_staff("s1")])
        assert len(result.assignments) == 1
        assert result.total_cost > 0

    def test_prefers_the_cheaper_qualified_person(self):
        staff = [_staff("expensive", rate="30.00"), _staff("cheap", rate="15.00")]
        result = build_roster([_requirement()], staff)
        assert result.assignments[0].staff_id == "cheap"

    def test_will_not_roster_someone_unavailable(self):
        result = build_roster([_requirement()], [_staff("s1", always=False)])
        assert result.assignments == []
        assert result.unfilled

    def test_respects_max_weekly_hours(self):
        # 6-hour shift, but only 4 hours of contract left.
        result = build_roster(
            [_requirement()], [_staff("s1", max_hours=44)], existing_hours={"s1": D("40")}
        )
        assert result.assignments == []

    def test_will_not_double_book_the_same_person(self):
        # Two servers needed on one shift, but only one person exists.
        result = build_roster([_requirement(headcount=2)], [_staff("s1")])
        assert len(result.assignments) == 1
        assert result.unfilled[0].required_headcount == 1

    def test_will_not_assign_overlapping_shifts(self):
        overlapping = [_requirement(start=10, end=16), _requirement(start=14, end=20)]
        result = build_roster(overlapping, [_staff("s1")])
        assert len(result.assignments) == 1

    def test_enforces_minimum_rest_between_days(self):
        # Closing at 23:00 then opening at 08:00 is 9 hours; the contract says 11.
        late = ShiftRequirement(
            business_date=DAY,
            role=ShiftRole.SERVER,
            starts_at=datetime.combine(DAY, time(17)),
            ends_at=datetime.combine(DAY, time(23)),
            required_headcount=1,
        )
        early_next = ShiftRequirement(
            business_date=DAY + timedelta(days=1),
            role=ShiftRole.SERVER,
            starts_at=datetime.combine(DAY + timedelta(days=1), time(8)),
            ends_at=datetime.combine(DAY + timedelta(days=1), time(14)),
            required_headcount=1,
        )
        person = _staff("s1")
        person.min_rest_hours = 11
        result = build_roster([late, early_next], [person])
        assert len(result.assignments) == 1, "must not break the minimum rest gap"
        assert result.unfilled

    def test_adequate_rest_is_allowed(self):
        late = ShiftRequirement(
            business_date=DAY,
            role=ShiftRole.SERVER,
            starts_at=datetime.combine(DAY, time(10)),
            ends_at=datetime.combine(DAY, time(16)),
            required_headcount=1,
        )
        next_day = ShiftRequirement(
            business_date=DAY + timedelta(days=1),
            role=ShiftRole.SERVER,
            starts_at=datetime.combine(DAY + timedelta(days=1), time(10)),
            ends_at=datetime.combine(DAY + timedelta(days=1), time(16)),
            required_headcount=1,
        )
        result = build_roster([late, next_day], [_staff("s1")])
        assert len(result.assignments) == 2

    def test_role_must_match(self):
        # A server cannot cover a line cook slot.
        result = build_roster([_requirement(role=ShiftRole.LINE_COOK)], [_staff("s1")])
        assert result.assignments == []

    def test_overtime_is_charged_at_a_premium(self):
        # 38 existing + a 6-hour shift crosses the 40-hour overtime threshold
        # while staying inside the 44-hour contract cap.
        result = build_roster(
            [_requirement()],
            [_staff("s1", rate="20.00", max_hours=44)],
            existing_hours={"s1": D("38")},
        )
        assert result.assignments[0].is_overtime
        assert result.assignments[0].cost > D("6") * D("20.00")

    def test_overtime_is_warned_about(self):
        result = build_roster(
            [_requirement()],
            [_staff("s1", rate="20.00", max_hours=44)],
            existing_hours={"s1": D("38")},
        )
        assert any("overtime" in w for w in result.warnings)

    def test_contract_cap_beats_overtime_premium(self):
        # Paying overtime is a soft cost; exceeding contracted hours is a hard
        # constraint. The cap must win, leaving the shift unfilled.
        result = build_roster(
            [_requirement()],
            [_staff("s1", rate="20.00", max_hours=44)],
            existing_hours={"s1": D("40")},
        )
        assert result.assignments == []
        assert result.unfilled

    def test_spreads_hours_to_avoid_overtime(self):
        # Same rate: the person with fewer hours should get the shift.
        staff = [_staff("busy", rate="15.00"), _staff("free", rate="15.00")]
        result = build_roster([_requirement()], staff, existing_hours={"busy": D("38")})
        assert result.assignments[0].staff_id == "free"

    def test_unfilled_shifts_are_explained(self):
        result = build_roster([_requirement()], [])
        assert result.unfilled
        assert "short 1 server" in result.warnings[0]

    def test_labour_pct(self):
        result = build_roster([_requirement()], [_staff("s1", rate="15.00")])
        assert result.labour_pct(D("1000")) == (result.total_cost / D("1000")).quantize(D("0.0001"))

    def test_labour_pct_with_no_revenue(self):
        result = build_roster([_requirement()], [_staff("s1")])
        assert result.labour_pct(D("0")) == D("0")

    def test_summary_is_readable(self):
        result = build_roster([_requirement()], [_staff("s1")])
        assert "shifts" in summarise_roster(result, D("1000"))

    def test_empty_requirements(self):
        result = build_roster([], [_staff("s1")])
        assert result.assignments == [] and result.total_cost == D("0")
