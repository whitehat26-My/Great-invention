from datetime import datetime

from restaurant_ai.db.models.enums import OrderChannel, Station
from restaurant_ai.domain.pacing import (
    STATION_CAPACITY,
    TicketRequest,
    plan_service,
    summarise_load,
)


def T(hour, minute=0):
    return datetime(2026, 8, 27, hour, minute)


def _req(line_id, station, course, seconds, order="o1", channel=OrderChannel.DINE_IN, qty=1):
    return TicketRequest(
        order_id=order,
        order_line_id=line_id,
        order_number=f"A-{order}",
        menu_item_name=f"Dish {line_id}",
        station=station,
        course=course,
        prep_seconds=seconds,
        quantity=qty,
        channel=channel,
    )


class TestCourseCoordination:
    def test_dishes_in_a_course_plate_together(self):
        # A 300s dish and a 660s dish in the same course must arrive at once,
        # so the fast one fires later rather than sitting under the pass.
        plan = plan_service(
            [_req("fast", Station.FRY, 2, 300), _req("slow", Station.GRILL, 2, 660)], T(19)
        )
        ready = {t.order_line_id: t.ready_at for t in plan.tickets}
        assert ready["fast"] == ready["slow"]

    def test_slower_dish_fires_first(self):
        plan = plan_service(
            [_req("fast", Station.FRY, 2, 300), _req("slow", Station.GRILL, 2, 660)], T(19)
        )
        fire = {t.order_line_id: t.fire_at for t in plan.tickets}
        assert fire["slow"] < fire["fast"]

    def test_courses_are_separated(self):
        plan = plan_service(
            [_req("starter", Station.FRY, 1, 300), _req("main", Station.GRILL, 2, 600)], T(19)
        )
        by_course = {t.course: t.ready_at for t in plan.tickets}
        assert by_course[2] > by_course[1]

    def test_nothing_fires_in_the_past(self):
        plan = plan_service([_req("a", Station.GRILL, 2, 900)], T(19))
        assert all(t.fire_at >= T(19) for t in plan.tickets)

    def test_multiple_covers_add_time_sublinearly(self):
        single = plan_service([_req("a", Station.GRILL, 2, 600, qty=1)], T(19))
        double = plan_service([_req("a", Station.GRILL, 2, 600, qty=2)], T(19))
        s = single.tickets[0].estimated_seconds
        d = double.tickets[0].estimated_seconds
        assert s < d < s * 2  # a hot pan cooks the second portion faster


class TestStationCapacity:
    def test_queue_beyond_capacity_is_delayed(self):
        limit = STATION_CAPACITY[Station.GRILL]
        requests = [_req(f"g{i}", Station.GRILL, 2, 600, order=f"o{i}") for i in range(limit + 3)]
        plan = plan_service(requests, T(19))
        fire_times = sorted({t.fire_at for t in plan.tickets})
        assert len(fire_times) > 1, "an oversubscribed station must stagger its tickets"

    def test_bottleneck_is_reported(self):
        requests = [_req(f"g{i}", Station.GRILL, 2, 600, order=f"o{i}") for i in range(12)]
        plan = plan_service(requests, T(19))
        assert plan.bottlenecks
        assert any("bottleneck" in w for w in plan.warnings)

    def test_slip_past_promised_plate_time_is_reported(self):
        # Contention can push a plate past the moment its table was promised;
        # the pass has to be told, or the table gets its food piecemeal.
        requests = [_req("dine", Station.GRILL, 2, 600, order="dine")]
        requests += [
            _req(f"d{i}", Station.GRILL, 2, 600, order=f"o{i}", channel=OrderChannel.DELIVERY)
            for i in range(14)
        ]
        plan = plan_service(requests, T(19))
        assert any(t.slip_seconds > 0 for t in plan.tickets)
        assert any("slipped past their promised plate time" in w for w in plan.warnings)

    def test_no_slip_when_the_station_is_quiet(self):
        plan = plan_service([_req("a", Station.GRILL, 2, 600)], T(19))
        assert all(t.slip_seconds == 0 for t in plan.tickets)

    def test_stations_are_independent(self):
        # A flood on the grill must not delay the fry station.
        grill = [_req(f"g{i}", Station.GRILL, 2, 600, order=f"o{i}") for i in range(12)]
        fry = [_req("f1", Station.FRY, 2, 300, order="ofry")]
        plan = plan_service(grill + fry, T(19))
        fry_ticket = next(t for t in plan.tickets if t.order_line_id == "f1")
        assert fry_ticket.slip_seconds == 0


class TestPlanBasics:
    def test_empty_input(self):
        plan = plan_service([], T(19))
        assert plan.tickets == [] and plan.loads == []

    def test_every_line_gets_a_ticket(self):
        requests = [_req(f"l{i}", Station.SAUTE, 2, 300, order=f"o{i}") for i in range(5)]
        assert len(plan_service(requests, T(19)).tickets) == 5

    def test_tickets_are_sequenced_within_a_station(self):
        requests = [_req(f"l{i}", Station.SAUTE, 2, 300, order=f"o{i}") for i in range(4)]
        sequences = [t.sequence for t in plan_service(requests, T(19)).tickets]
        assert len(set(sequences)) == len(sequences)

    def test_long_wait_is_warned(self):
        requests = [_req(f"g{i}", Station.GRILL, 2, 900, order=f"o{i}") for i in range(16)]
        plan = plan_service(requests, T(19))
        assert any("Quote longer wait times" in w for w in plan.warnings)

    def test_summarise_load(self):
        plan = plan_service([_req("a", Station.GRILL, 2, 600)], T(19))
        assert "grill" in summarise_load(plan)

    def test_summarise_empty(self):
        assert "No active tickets" in summarise_load(plan_service([], T(19)))
