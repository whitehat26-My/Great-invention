"""Table allocation and turnover.

Two jobs: find the smallest table that fits a party without blocking a bigger
one it might have served, and flag tables sitting past their expected turn.

Turn time scales with party size — six people take materially longer than two —
so a fixed 90-minute slot either wastes covers on small tables or runs late on
large ones.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal

# Party size -> minutes at table, including turnaround for the next booking.
TURN_MINUTES: dict[int, int] = {1: 45, 2: 75, 3: 90, 4: 95, 5: 110, 6: 120}
LARGE_PARTY_MINUTES = 135
TURNAROUND_MINUTES = 15


@dataclass
class TableSlot:
    """A table and the bookings already on it for the day."""

    table_id: str
    label: str
    seats: int
    section: str
    min_party: int = 1
    is_combinable: bool = False
    busy: list[tuple[datetime, datetime]] = field(default_factory=list)

    def is_free(self, start: datetime, end: datetime) -> bool:
        return all(end <= b_start or start >= b_end for b_start, b_end in self.busy)


@dataclass
class SeatingOption:
    table_id: str
    label: str
    seats: int
    section: str
    starts_at: datetime
    ends_at: datetime
    spare_seats: int
    fit_score: Decimal


@dataclass
class OverrunTable:
    table_id: str
    label: str
    party_size: int
    seated_at: datetime
    expected_end_at: datetime
    minutes_over: int
    next_booking_at: datetime | None
    severity: str  # "watch" | "late" | "blocking"
    suggestion: str


def turn_minutes(party_size: int, include_turnaround: bool = True) -> int:
    """Expected minutes a party of this size occupies a table."""
    base = TURN_MINUTES.get(party_size, LARGE_PARTY_MINUTES if party_size > 6 else 90)
    return base + (TURNAROUND_MINUTES if include_turnaround else 0)


def expected_end(start: datetime, party_size: int) -> datetime:
    return start + timedelta(minutes=turn_minutes(party_size))


def find_availability(
    tables: list[TableSlot],
    party_size: int,
    requested_at: datetime,
    window_minutes: int = 90,
    step_minutes: int = 15,
    max_options: int = 5,
) -> list[SeatingOption]:
    """Offer seating options at or near the requested time.

    Options are ranked by how tightly the party fits the table and how close the
    slot is to what was asked for. Seating two people at a six-top when the
    dining room is filling is how you turn away a party of six later, so spare
    seats are penalised heavily.
    """
    if party_size <= 0:
        return []

    duration = timedelta(minutes=turn_minutes(party_size))
    offsets = [0]
    for step in range(step_minutes, window_minutes + 1, step_minutes):
        offsets.extend([-step, step])

    options: list[SeatingOption] = []
    seen: set[tuple[str, datetime]] = set()

    for offset in offsets:
        start = requested_at + timedelta(minutes=offset)
        end = start + duration
        for table in tables:
            if table.seats < party_size or party_size < table.min_party:
                continue
            if not table.is_free(start, end):
                continue
            key = (table.table_id, start)
            if key in seen:
                continue
            seen.add(key)

            spare = table.seats - party_size
            # Lower is better: spare seats dominate, time drift breaks ties.
            score = Decimal(spare) * Decimal("10") + Decimal(abs(offset)) / Decimal("15")
            options.append(
                SeatingOption(
                    table_id=table.table_id,
                    label=table.label,
                    seats=table.seats,
                    section=table.section,
                    starts_at=start,
                    ends_at=end,
                    spare_seats=spare,
                    fit_score=score.quantize(Decimal("0.01")),
                )
            )

    options.sort(key=lambda o: (o.fit_score, abs((o.starts_at - requested_at).total_seconds())))
    return options[:max_options]


def best_table(
    tables: list[TableSlot], party_size: int, requested_at: datetime
) -> SeatingOption | None:
    options = find_availability(tables, party_size, requested_at, window_minutes=0)
    return options[0] if options else None


def find_overruns(
    seated: list[tuple[str, str, int, datetime]],
    now: datetime,
    upcoming: dict[str, datetime] | None = None,
    watch_threshold_minutes: int = 10,
) -> list[OverrunTable]:
    """Flag tables past their expected turn.

    A table running late only actually matters when someone is waiting for it,
    so severity escalates when there is a booking behind it.
    """
    upcoming = upcoming or {}
    overruns: list[OverrunTable] = []

    for table_id, label, party_size, seated_at in seated:
        expected = expected_end(seated_at, party_size)
        minutes_over = int((now - expected).total_seconds() // 60)
        if minutes_over < watch_threshold_minutes:
            continue

        next_booking = upcoming.get(table_id)
        if next_booking is not None and now >= next_booking - timedelta(minutes=15):
            severity = "blocking"
            suggestion = (
                f"{label} is {minutes_over} min over and the next booking is at "
                f"{next_booking:%H:%M}. Offer the bar for coffee and dessert, or move the "
                f"incoming party to another table now."
            )
        elif minutes_over >= 30:
            severity = "late"
            suggestion = (
                f"{label} is {minutes_over} min over expected turn. Drop the bill "
                f"proactively; nothing is waiting on it yet."
            )
        else:
            severity = "watch"
            suggestion = f"{label} is {minutes_over} min over. Worth a check-in."

        overruns.append(
            OverrunTable(
                table_id=table_id,
                label=label,
                party_size=party_size,
                seated_at=seated_at,
                expected_end_at=expected,
                minutes_over=minutes_over,
                next_booking_at=next_booking,
                severity=severity,
                suggestion=suggestion,
            )
        )

    order = {"blocking": 0, "late": 1, "watch": 2}
    overruns.sort(key=lambda o: (order[o.severity], -o.minutes_over))
    return overruns


def utilisation(
    tables: list[TableSlot], service_start: datetime, service_end: datetime
) -> dict[str, Decimal]:
    """Fraction of the service window each table is booked. Feeds the daily report."""
    total_minutes = Decimal((service_end - service_start).total_seconds()) / Decimal(60)
    if total_minutes <= 0:
        return {}

    result: dict[str, Decimal] = {}
    for table in tables:
        booked = Decimal("0")
        for start, end in table.busy:
            overlap_start = max(start, service_start)
            overlap_end = min(end, service_end)
            if overlap_end > overlap_start:
                booked += Decimal((overlap_end - overlap_start).total_seconds()) / Decimal(60)
        result[table.label] = (booked / total_minutes).quantize(Decimal("0.001"))
    return result
