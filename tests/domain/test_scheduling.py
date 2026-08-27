from datetime import date, datetime, time, timedelta
from decimal import Decimal

from restaurant_ai.db.models.enums import ShiftRole
from restaurant_ai.domain.scheduling import (
    MAX_SHIFT_HOURS,
    MIN_SHIFT_HOURS,
    ShiftRequirement,
    StaffMember,
    build_requirements,
    build_roster,
    hourly_headcount,
    pack_shifts,
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


# A realistic trading curve: a lunch peak, a dead mid-afternoon, a bigger
# dinner peak. Sizing against this rather than a block average is the whole
# point of the shift shaper.
SHAPE = {
    11: D("0.05"),
    12: D("0.11"),
    13: D("0.09"),
    14: D("0.04"),
    15: D("0.02"),
    16: D("0.03"),
    17: D("0.05"),
    18: D("0.10"),
    19: D("0.15"),
    20: D("0.16"),
    21: D("0.11"),
    22: D("0.09"),
}


def _hours(reqs) -> float:
    return sum(float(r.hours) * r.required_headcount for r in reqs)


class TestHourlyHeadcount:
    def test_sizes_against_the_peak_not_the_average(self):
        # 12:00 runs at nearly six times 15:00. A model that staffs to the
        # day's average is short at the peak and idle in the lull.
        covers = {11: D("8"), 12: D("32"), 15: D("3")}
        need = hourly_headcount(covers, ShiftRole.SERVER)
        assert need[12] > need[15]

    def test_essential_roles_are_present_whenever_open(self):
        # You cannot trade without a manager, a chef, a line and a floor.
        covers = dict.fromkeys(range(11, 23), D("1"))
        for role in (ShiftRole.MANAGER, ShiftRole.CHEF, ShiftRole.LINE_COOK, ShiftRole.SERVER):
            need = hourly_headcount(covers, role)
            assert all(need.get(h, 0) >= 1 for h in range(11, 23)), role

    def test_a_discretionary_role_is_not_summoned_by_one_guest(self):
        # ceil() on a trickle is what put a dedicated host, barista and porter
        # on the floor for a whole thirteen-hour day.
        covers = dict.fromkeys(range(11, 23), D("2"))
        for role in (ShiftRole.HOST, ShiftRole.BARISTA, ShiftRole.KITCHEN_PORTER):
            assert hourly_headcount(covers, role) == {}, role

    def test_a_discretionary_role_appears_once_the_work_does(self):
        covers = dict.fromkeys(range(11, 23), D("400"))
        assert hourly_headcount(covers, ShiftRole.HOST)

    def test_the_chef_covers_prep_and_breakdown(self):
        covers = dict.fromkeys(range(11, 23), D("20"))
        chef = hourly_headcount(covers, ShiftRole.CHEF)
        assert min(chef) < 11, "someone has to prep before service"
        assert max(chef) >= 22, "someone has to close the kitchen"

    def test_the_line_is_not_paid_to_watch_prep(self):
        covers = dict.fromkeys(range(11, 23), D("20"))
        assert min(hourly_headcount(covers, ShiftRole.LINE_COOK)) >= 11


class TestPackShifts:
    def test_covers_every_hour_of_need(self):
        need = {11: 1, 12: 2, 13: 2, 14: 1, 15: 1, 16: 1, 17: 1, 18: 2, 19: 2}
        shifts = pack_shifts(need)
        for hour, wanted in need.items():
            covering = sum(1 for s, e in shifts if s <= hour < e)
            assert covering >= wanted, f"hour {hour} short: {covering} of {wanted}"

    def test_never_calls_anyone_in_for_a_sliver(self):
        need = {11: 1, 12: 1, 13: 1, 14: 1, 15: 1, 16: 1, 17: 1, 18: 1, 19: 2, 20: 2}
        for start, end in pack_shifts(need):
            assert end - start >= MIN_SHIFT_HOURS

    def test_respects_the_maximum_shift_length(self):
        need = dict.fromkeys(range(8, 24), 1)
        for start, end in pack_shifts(need):
            assert end - start <= MAX_SHIFT_HOURS

    def test_a_flat_day_needs_no_extra_bodies(self):
        # One person's worth of work all day is one person, spread over shifts.
        need = dict.fromkeys(range(11, 20), 1)
        shifts = pack_shifts(need)
        assert all(sum(1 for s, e in shifts if s <= h < e) == 1 for h in need)

    def test_empty_need(self):
        assert pack_shifts({}) == []


class TestBuildRequirements:
    def test_follows_the_shape_of_the_day(self):
        # More cover across the dinner peak than the dead mid-afternoon.
        reqs = build_requirements(DAY, 200, SHAPE, roles=[ShiftRole.SERVER])

        def on_at(hour: int) -> int:
            return sum(
                r.required_headcount
                for r in reqs
                if r.starts_at.hour <= hour < (r.ends_at.hour or 24)
            )

        assert on_at(20) > on_at(15)

    def test_scales_with_volume(self):
        quiet = _hours(build_requirements(DAY, 80, SHAPE))
        busy = _hours(build_requirements(DAY, 400, SHAPE))
        assert busy > quiet

    def test_labour_stays_in_a_plausible_band_on_a_normal_day(self):
        # The whole reason for shaping shifts. A two-block roster put 98 hours
        # on a 154-cover day, which is over half of revenue in wages.
        reqs = build_requirements(DAY, 154, SHAPE)
        hours = _hours(reqs)
        assert 45 <= hours <= 75, f"{hours} hours for 154 covers is not plausible"

    def test_a_quiet_day_is_dominated_by_the_minimum(self):
        # Correct, not a bug: a restaurant open twelve hours for sixty covers
        # loses money on labour, and the roster should show that rather than
        # pretend otherwise by understaffing.
        quiet = _hours(build_requirements(DAY, 60, SHAPE))
        busy = _hours(build_requirements(DAY, 154, SHAPE))
        assert quiet / 60 > busy / 154, "labour per cover must be worse when quiet"

    def test_identical_windows_are_merged(self):
        reqs = build_requirements(DAY, 400, SHAPE, roles=[ShiftRole.SERVER])
        keys = [(r.role, r.starts_at, r.ends_at) for r in reqs]
        assert len(keys) == len(set(keys)), "one line per window, with a headcount"

    def test_no_shift_is_shorter_than_the_minimum(self):
        for covers in (60, 154, 400):
            for r in build_requirements(DAY, covers, SHAPE):
                assert r.hours >= MIN_SHIFT_HOURS, f"{r.role} {r.starts_at}-{r.ends_at}"

    def test_requirements_are_chronological(self):
        reqs = build_requirements(DAY, 154, SHAPE)
        assert reqs == sorted(reqs, key=lambda r: (r.starts_at, r.role.value))


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

    def test_will_not_hand_one_person_a_double(self):
        """Two adjacent shifts are not an overlap, but they are a fourteen-hour day.

        The weekly cap allows it and the overlap check passes, because 19:00 is
        not strictly before 19:00. Without a daily ceiling the fitter happily
        gave one chef the 10:00-19:00 and then the 19:00-00:00 close.
        """
        long_shift = ShiftRequirement(
            business_date=DAY,
            role=ShiftRole.CHEF,
            starts_at=datetime.combine(DAY, time(10)),
            ends_at=datetime.combine(DAY, time(19)),
            required_headcount=1,
        )
        close = ShiftRequirement(
            business_date=DAY,
            role=ShiftRole.CHEF,
            starts_at=datetime.combine(DAY, time(19)),
            ends_at=datetime.combine(DAY, time(23)),
            required_headcount=1,
        )
        only_one = [_staff("solo", role=ShiftRole.CHEF, max_hours=60)]
        result = build_roster([long_shift, close], only_one)

        assert len(result.assignments) == 1, "the same person must not work back to back"
        assert result.unfilled

    def test_a_second_person_takes_the_close(self):
        long_shift = ShiftRequirement(
            business_date=DAY,
            role=ShiftRole.CHEF,
            starts_at=datetime.combine(DAY, time(10)),
            ends_at=datetime.combine(DAY, time(19)),
            required_headcount=1,
        )
        close = ShiftRequirement(
            business_date=DAY,
            role=ShiftRole.CHEF,
            starts_at=datetime.combine(DAY, time(19)),
            ends_at=datetime.combine(DAY, time(23)),
            required_headcount=1,
        )
        pair = [
            _staff("a", role=ShiftRole.CHEF, rate="25.00"),
            _staff("b", role=ShiftRole.CHEF, rate="26.00"),
        ]
        result = build_roster([long_shift, close], pair)
        assert len({a.staff_id for a in result.assignments}) == 2

    def test_a_split_shift_within_the_daily_cap_is_allowed(self):
        # Four on, a break, four on is normal restaurant work.
        morning = _requirement(start=11, end=15)
        evening = _requirement(start=19, end=23)
        result = build_roster([morning, evening], [_staff("s1")])
        assert len(result.assignments) == 2

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
        assert any("server" in w and "unfilled" in w for w in result.warnings)

    def test_a_role_short_every_day_reads_as_a_hiring_gap(self):
        """One uncovered shift is a rostering problem; the same one all week is not.

        Repeating an identical line per day buries the finding. The week needs
        more manager-hours than the manager is contracted for, and saying that
        once with the numbers is what an owner can act on.
        """
        requirements = [
            _requirement(role=ShiftRole.MANAGER, start=11, end=20, day=DAY + timedelta(days=n))
            for n in range(6)
        ] + [
            _requirement(role=ShiftRole.MANAGER, start=19, end=23, day=DAY + timedelta(days=n))
            for n in range(6)
        ]
        one_manager = [_staff("m1", role=ShiftRole.MANAGER, max_hours=45)]
        result = build_roster(requirements, one_manager)

        assert result.unfilled
        gap = next((w for w in result.warnings if "structurally short" in w), None)
        assert gap is not None, result.warnings
        assert "manager" in gap
        assert "Hiring is the fix" in gap

    def test_a_one_off_clash_is_not_called_a_hiring_gap(self):
        # Plenty of cover on paper; this one just does not fit.
        result = build_roster([_requirement()], [_staff("s1", always=False)])
        assert result.unfilled
        assert not any("structurally short" in w for w in result.warnings)

    def test_the_summary_leads_with_the_gap_not_the_list(self):
        requirements = [
            _requirement(role=ShiftRole.MANAGER, start=11, end=20, day=DAY + timedelta(days=n))
            for n in range(6)
        ]
        summary = summarise_roster(build_roster(requirements, []), D("10000"))
        assert "hours uncovered" in summary
        # A handful of examples, not one line per unfilled shift.
        assert summary.count("e.g.") <= 3

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
