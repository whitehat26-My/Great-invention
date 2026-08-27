from datetime import datetime, timedelta

from restaurant_ai.domain.tables import (
    TableSlot,
    best_table,
    expected_end,
    find_availability,
    find_overruns,
    turn_minutes,
    utilisation,
)


def T(hour, minute=0):
    return datetime(2026, 8, 27, hour, minute)


def _room() -> list[TableSlot]:
    return [
        TableSlot("t1", "T1", 2, "window"),
        TableSlot("t2", "T2", 2, "window"),
        TableSlot("t4", "T4", 4, "main"),
        TableSlot("t8", "T8", 6, "main"),
        TableSlot("t10", "T10", 8, "private"),
    ]


class TestTurnTimes:
    def test_larger_parties_take_longer(self):
        assert turn_minutes(2) < turn_minutes(4) < turn_minutes(6) < turn_minutes(10)

    def test_includes_turnaround(self):
        assert turn_minutes(2, include_turnaround=True) > turn_minutes(2, include_turnaround=False)

    def test_expected_end(self):
        assert expected_end(T(19), 2) == T(19) + timedelta(minutes=turn_minutes(2))


class TestFindAvailability:
    def test_prefers_the_tightest_fit(self):
        # A pair must not be seated at the eight-top while a two-top is free.
        options = find_availability(_room(), 2, T(19))
        assert options[0].seats == 2

    def test_never_offers_a_table_that_is_too_small(self):
        assert all(o.seats >= 6 for o in find_availability(_room(), 6, T(19)))

    def test_skips_occupied_tables(self):
        room = _room()
        room[0].busy.append((T(18, 30), T(20, 30)))
        options = find_availability(room, 2, T(19), window_minutes=0)
        assert all(o.table_id != "t1" for o in options)

    def test_offers_nearby_times_when_exact_slot_is_taken(self):
        room = [TableSlot("t1", "T1", 2, "window")]
        room[0].busy.append((T(19), T(20, 30)))
        options = find_availability(room, 2, T(19), window_minutes=120)
        assert options, "should offer an alternative time"
        assert all(not (o.starts_at < T(20, 30) and T(19) < o.ends_at) for o in options)

    def test_full_room_returns_nothing(self):
        room = _room()
        for table in room:
            table.busy.append((T(17), T(23)))
        assert find_availability(room, 2, T(19)) == []

    def test_zero_party_size(self):
        assert find_availability(_room(), 0, T(19)) == []

    def test_respects_minimum_party(self):
        room = [TableSlot("t10", "T10", 8, "private", min_party=6)]
        assert find_availability(room, 2, T(19)) == []
        assert find_availability(room, 6, T(19))

    def test_respects_max_options(self):
        assert len(find_availability(_room(), 2, T(19), max_options=2)) <= 2

    def test_best_table_returns_the_top_option(self):
        assert best_table(_room(), 2, T(19)).seats == 2

    def test_best_table_when_nothing_free(self):
        room = _room()
        for table in room:
            table.busy.append((T(17), T(23)))
        assert best_table(room, 2, T(19)) is None


class TestOverruns:
    def test_no_flag_before_expected_turn(self):
        assert find_overruns([("t1", "T1", 2, T(19))], now=T(19, 30)) == []

    def test_flags_a_table_running_late(self):
        seated_at = T(18)
        overruns = find_overruns([("t1", "T1", 2, seated_at)], now=T(20, 30))
        assert len(overruns) == 1
        assert overruns[0].minutes_over > 0

    def test_escalates_when_a_booking_is_waiting(self):
        overruns = find_overruns([("t8", "T8", 6, T(18))], now=T(20, 50), upcoming={"t8": T(21)})
        assert overruns[0].severity == "blocking"
        assert "next booking" in overruns[0].suggestion

    def test_late_without_a_waiting_booking_is_lower_priority(self):
        overruns = find_overruns([("t8", "T8", 6, T(18))], now=T(21, 10))
        assert overruns[0].severity == "late"
        assert "nothing is waiting" in overruns[0].suggestion

    def test_blocking_sorts_first(self):
        overruns = find_overruns(
            [("t1", "T1", 2, T(17)), ("t8", "T8", 6, T(18))],
            now=T(20, 50),
            upcoming={"t8": T(21)},
        )
        assert overruns[0].severity == "blocking"

    def test_every_overrun_carries_an_action(self):
        overruns = find_overruns([("t1", "T1", 2, T(17))], now=T(20))
        assert all(len(o.suggestion) > 20 for o in overruns)


def test_utilisation():
    room = [TableSlot("t1", "T1", 2, "window")]
    room[0].busy.append((T(18), T(20)))  # 2 of 6 hours
    result = utilisation(room, T(17), T(23))
    assert result["T1"] == __import__("decimal").Decimal("0.333")


def test_utilisation_zero_window():
    assert utilisation(_room(), T(19), T(19)) == {}
