"""Kitchen order routing and pacing.

Two problems, and the second is the one that actually ruins service.

Routing: each order line belongs to a station, determined by its recipe.

Pacing: a table's plates must land together. Different dishes take different
times, so if everything fires at once the fast items sit under the pass going
cold while the slow one finishes. The fix is to back-calculate from a target
serve time and fire each item at (target - its cook time), which is what an
expediting chef does in their head.

On top of that, stations have finite hands. Once a station's queue exceeds its
concurrent capacity, work spills into a later slot, and a flood of takeaway
tickets must not push a seated table's mains into next week — so dine-in gets
priority within the same course.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from restaurant_ai.db.models.enums import OrderChannel, Station

# How many tickets a station can genuinely work at once.
STATION_CAPACITY: dict[Station, int] = {
    Station.GRILL: 4,
    Station.FRY: 3,
    Station.SAUTE: 5,
    Station.COLD: 3,
    Station.PASTRY: 2,
    Station.BAR: 3,
    Station.EXPO: 6,
}

# Channels that must not be starved by a rush on another channel.
CHANNEL_PRIORITY: dict[OrderChannel, int] = {
    OrderChannel.DINE_IN: 0,
    OrderChannel.DRIVE_THRU: 1,
    OrderChannel.KIOSK: 2,
    OrderChannel.TAKEAWAY: 2,
    OrderChannel.PHONE: 3,
    OrderChannel.DELIVERY: 3,
}

COURSE_GAP_MINUTES = 12


@dataclass
class TicketRequest:
    """One order line awaiting routing."""

    order_id: str
    order_line_id: str
    order_number: str
    menu_item_name: str
    station: Station
    course: int
    prep_seconds: int
    quantity: int = 1
    channel: OrderChannel = OrderChannel.DINE_IN
    placed_at: datetime | None = None
    modifiers: str | None = None


@dataclass
class ScheduledTicket:
    order_id: str
    order_line_id: str
    order_number: str
    menu_item_name: str
    station: Station
    course: int
    sequence: int
    fire_at: datetime
    ready_at: datetime
    estimated_seconds: int
    channel: OrderChannel
    modifiers: str | None = None
    # What the course-coordination pass promised, before station capacity was
    # applied. The gap between this and ready_at is the slip the pass must know
    # about: a plate that misses it arrives after the rest of the table's food.
    target_ready_at: datetime | None = None

    @property
    def slip_seconds(self) -> int:
        if self.target_ready_at is None:
            return 0
        return max(0, int((self.ready_at - self.target_ready_at).total_seconds()))


@dataclass
class StationLoad:
    station: Station
    ticket_count: int
    total_seconds: int
    capacity: int
    peak_demand: int
    """Most tickets that wanted to be cooking at once, before throttling."""
    delayed_count: int = 0
    """Tickets pushed back because every hand was busy."""

    @property
    def minutes(self) -> int:
        return self.total_seconds // 60

    @property
    def is_bottleneck(self) -> bool:
        # Measured against demand, not against the throttled result: capacity
        # is applied by holding in_flight at the limit, so the post-throttle
        # queue can never exceed it and would never flag anything.
        return self.delayed_count > 0 or self.peak_demand > self.capacity


@dataclass
class PacingPlan:
    tickets: list[ScheduledTicket] = field(default_factory=list)
    loads: list[StationLoad] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def bottlenecks(self) -> list[StationLoad]:
        return [load for load in self.loads if load.is_bottleneck]


def plan_service(
    requests: list[TicketRequest],
    now: datetime,
    capacity: dict[Station, int] | None = None,
) -> PacingPlan:
    """Route and sequence a batch of order lines into a firing plan."""
    if not requests:
        return PacingPlan()

    capacity = {**STATION_CAPACITY, **(capacity or {})}
    plan = PacingPlan()

    # Group by order so a table's courses can be coordinated against each other.
    by_order: dict[str, list[TicketRequest]] = {}
    for request in requests:
        by_order.setdefault(request.order_id, []).append(request)

    scheduled: list[ScheduledTicket] = []
    for order_id, lines in by_order.items():
        scheduled.extend(_schedule_order(order_id, lines, now))

    # Apply station capacity: within a station, work the queue in priority order
    # and push anything beyond capacity into the next slot.
    station_queues: dict[Station, list[ScheduledTicket]] = {}
    for ticket in scheduled:
        station_queues.setdefault(ticket.station, []).append(ticket)

    final: list[ScheduledTicket] = []
    for station, tickets in station_queues.items():
        tickets.sort(
            key=lambda t: (
                t.fire_at,
                t.course,
                CHANNEL_PRIORITY.get(t.channel, 5),
                t.order_number,
            )
        )
        limit = capacity.get(station, 3)
        peak_demand = _peak_concurrent(tickets)
        delayed = 0
        in_flight: list[datetime] = []  # ready_at of tickets currently cooking

        for index, ticket in enumerate(tickets):
            in_flight = [ready for ready in in_flight if ready > ticket.fire_at]
            if len(in_flight) >= limit:
                # Station is full: wait for the earliest hand to come free.
                in_flight.sort()
                delayed_to = in_flight[0]
                delay = int((delayed_to - ticket.fire_at).total_seconds())
                if delay > 0:
                    ticket.fire_at = delayed_to
                    ticket.ready_at = delayed_to + timedelta(seconds=ticket.estimated_seconds)
                    delayed += 1
                in_flight.pop(0)
            in_flight.append(ticket.ready_at)
            ticket.sequence = index
            final.append(ticket)

        total_seconds = sum(t.estimated_seconds for t in tickets)
        load = StationLoad(
            station=station,
            ticket_count=len(tickets),
            total_seconds=total_seconds,
            capacity=limit,
            peak_demand=peak_demand,
            delayed_count=delayed,
        )
        plan.loads.append(load)
        if load.is_bottleneck:
            plan.warnings.append(
                f"{station.value} is the bottleneck: {load.ticket_count} tickets, peak demand "
                f"{peak_demand} against capacity {limit}, {delayed} ticket(s) held back "
                f"({load.minutes} min of work). Pull a hand across or stagger takeaway tickets."
            )

    final.sort(key=lambda t: (t.fire_at, t.station.value))
    plan.tickets = final
    plan.loads.sort(key=lambda load: load.total_seconds, reverse=True)

    # Station contention can push a plate past the time its table was promised.
    # Dine-in slips matter most: the rest of that table's food is already up.
    slipped = [t for t in final if t.slip_seconds >= 120]
    if slipped:
        worst = max(slipped, key=lambda t: t.slip_seconds)
        dine_in_slips = [t for t in slipped if t.channel == OrderChannel.DINE_IN]
        plan.warnings.append(
            f"{len(slipped)} ticket(s) slipped past their promised plate time "
            f"({len(dine_in_slips)} dine-in). Worst: {worst.menu_item_name} on "
            f"{worst.order_number} is {worst.slip_seconds // 60} min late off "
            f"{worst.station.value}. Courses for those tables will not land together."
        )

    latest = max((t.ready_at for t in final), default=now)
    if (latest - now) > timedelta(minutes=45):
        plan.warnings.append(
            f"Last ticket does not plate until {latest:%H:%M}, "
            f"{int((latest - now).total_seconds() // 60)} min out. Quote longer wait times."
        )
    return plan


def _schedule_order(
    order_id: str, lines: list[TicketRequest], now: datetime
) -> list[ScheduledTicket]:
    """Sequence one order so each course plates together.

    Within a course, the slowest dish sets the serve time and everything else is
    fired late enough to be ready at the same moment.
    """
    tickets: list[ScheduledTicket] = []
    by_course: dict[int, list[TicketRequest]] = {}
    for line in lines:
        by_course.setdefault(line.course, []).append(line)

    course_start = now
    for course in sorted(by_course):
        course_lines = by_course[course]
        longest = max(_seconds_for(line) for line in course_lines)
        serve_at = course_start + timedelta(seconds=longest)

        for line in course_lines:
            seconds = _seconds_for(line)
            # Fire late enough that this dish is ready exactly at serve time.
            fire_at = serve_at - timedelta(seconds=seconds)
            tickets.append(
                ScheduledTicket(
                    order_id=order_id,
                    order_line_id=line.order_line_id,
                    order_number=line.order_number,
                    menu_item_name=line.menu_item_name,
                    station=line.station,
                    course=course,
                    sequence=0,
                    fire_at=max(fire_at, now),
                    ready_at=serve_at,
                    estimated_seconds=seconds,
                    channel=line.channel,
                    modifiers=line.modifiers,
                    target_ready_at=serve_at,
                )
            )
        # The next course starts after this one has been eaten, not plated.
        course_start = serve_at + timedelta(minutes=COURSE_GAP_MINUTES)

    return tickets


def _seconds_for(line: TicketRequest) -> int:
    """Cook time for a line.

    Cooking two of something is not twice the work — the pan is already hot —
    so additional covers add half the base time each.
    """
    if line.quantity <= 1:
        return line.prep_seconds
    return int(line.prep_seconds * (1 + (line.quantity - 1) * 0.5))


def _peak_concurrent(tickets: list[ScheduledTicket]) -> int:
    """Most tickets wanting to cook simultaneously, from their unthrottled times.

    A sweep over start/end boundaries: +1 when a ticket wants to fire, -1 when
    it plates. The high-water mark is the demand the station actually faces.
    """
    events: list[tuple[datetime, int]] = []
    for ticket in tickets:
        events.append((ticket.fire_at, 1))
        events.append((ticket.ready_at, -1))
    # Process departures before arrivals at the same instant: a hand that frees
    # up exactly as the next ticket fires is available for it.
    events.sort(key=lambda e: (e[0], e[1]))

    current = peak = 0
    for _, delta in events:
        current += delta
        peak = max(peak, current)
    return peak


def summarise_load(plan: PacingPlan) -> str:
    """One-line station summary for the KDS header."""
    if not plan.loads:
        return "No active tickets."
    parts = [
        f"{load.station.value} {load.ticket_count} tix/{load.minutes}min"
        + ("*" if load.is_bottleneck else "")
        for load in plan.loads
    ]
    return " | ".join(parts)
