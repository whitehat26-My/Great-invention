"""A full simulated service day.

Replays a day of trading through the same code production would use: orders go
in as signed webhook payloads through the real handler, the scheduled agents
fire at their real times, and approvals go through the real gate. Nothing takes
a shortcut into the database.

That is what makes this useful as a regression test rather than a demo. If the
BOM explosion breaks, or reconciliation stops balancing, or an agent starts
proposing nonsense, a simulated day catches it — and because the simulators are
seeded from the date, the same day always replays identically.

Time is moved with ``clock.set_frozen`` so agents scheduled for 06:00 and 23:45
both run within a few seconds, without any agent knowing time is not real.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from typing import Any

from restaurant_ai import clock
from restaurant_ai.db.base import session_scope
from restaurant_ai.logging_setup import get_logger

log = get_logger(__name__)
ZERO = Decimal("0")

# When each agent runs during the simulated day, in local time.
TIMELINE: list[tuple[time, str, str]] = [
    (time(5, 30), "shift_scheduling", "Publish today's roster"),
    (time(6, 0), "prep_forecaster", "Morning prep forecast"),
    (time(7, 0), "stock_reorder", "Morning reorder sweep"),
    (time(9, 30), "supplier_invoice", "Deliveries and invoice matching"),
    (time(11, 0), "social_content", "Content and win-back offers"),
    (time(15, 0), "stock_reorder", "Afternoon reorder sweep"),
    (time(17, 0), "order_pacing", "Fire the dinner tickets"),
    (time(21, 0), "reputation", "Evening review sweep"),
    (time(23, 0), "__payroll__", "Record hours worked"),
    (time(23, 30), "bookkeeping", "Reconcile the day and post journals"),
    (time(23, 45), "daily_performance", "End-of-day report"),
]


@dataclass
class StepResult:
    at: datetime
    label: str
    kind: str  # agent | trading | approval
    status: str
    detail: str = ""
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass
class SimulationResult:
    business_date: date
    steps: list[StepResult] = field(default_factory=list)
    orders_ingested: int = 0
    orders_rejected: int = 0
    approvals_requested: int = 0
    approvals_approved: int = 0
    report: dict[str, Any] = field(default_factory=dict)

    @property
    def failures(self) -> list[StepResult]:
        return [s for s in self.steps if s.status == "failed"]


def simulate_day(
    business_date: date | None = None,
    auto_approve: bool = False,
    covers: int | None = None,
    on_step=None,
) -> SimulationResult:
    """Replay one full service day.

    ``auto_approve`` answers approval gates automatically, which is what makes
    the simulation usable unattended; without it the day still runs and the
    approvals are left pending for a human.
    """
    business_date = business_date or clock.today()
    result = SimulationResult(business_date=business_date)

    def emit(step: StepResult) -> None:
        result.steps.append(step)
        if on_step is not None:
            on_step(step)

    from restaurant_ai.integrations import get_integrations, reset_integrations
    from restaurant_ai.integrations.fakes import FakePOS

    reset_integrations()
    # Explicitly the simulator, not whatever POS_PROVIDER resolves to: replaying
    # a day means generating one, which is not something a live POS can do (and
    # pointing this at a real POS would be a good way to invent sales).
    configured = get_integrations().pos
    pos: FakePOS = (
        FakePOS(base_covers=covers) if covers or not isinstance(configured, FakePOS) else configured
    )
    orders = pos.generate_day(business_date)

    # Build a single timeline of everything that happens, in order, so agents
    # and trading interleave the way they actually would.
    timeline: list[tuple[datetime, str, Any]] = []
    for slot, agent_name, label in TIMELINE:
        at = datetime.combine(business_date, slot, tzinfo=clock.local_tz())
        timeline.append((at, "agent", (agent_name, label)))
    for order in orders:
        timeline.append((order.placed_at, "order", order))
    timeline.sort(key=lambda entry: entry[0])

    log.info(
        "simulation starting",
        business_date=business_date.isoformat(),
        orders=len(orders),
        agent_steps=len(TIMELINE),
    )

    try:
        for at, kind, item in timeline:
            clock.set_frozen(at)
            if kind == "order":
                _ingest_order(item, result, emit)
            else:
                agent_name, label = item
                if agent_name == "__payroll__":
                    _record_hours(business_date, at, emit)
                    continue
                _run_agent_step(agent_name, label, at, business_date, auto_approve, result, emit)
    finally:
        clock.set_frozen(None)

    result.report = _load_report(business_date)
    return result


def _ingest_order(order, result: SimulationResult, emit) -> None:
    """Push one order through the real webhook handler."""
    from restaurant_ai.api.ingest import mark_processed, record_event
    from restaurant_ai.api.webhooks.handlers import handle_pos_order

    payload = {
        "event_id": f"SIM-{order.external_id}",
        "external_id": order.external_id,
        "order_number": order.external_id,
        "channel": order.channel,
        "party_size": order.party_size,
        "placed_at": order.placed_at.isoformat(),
        "payment_method": order.payment_method,
        "processor_ref": order.processor_ref,
        "delivery_platform": order.delivery_platform,
        "lines": [
            {
                "sku": line.sku,
                "quantity": line.quantity,
                "unit_price": str(line.unit_price),
                "course": line.course,
                "modifiers": line.modifiers,
            }
            for line in order.lines
        ],
    }

    ingest = record_event("pos", "pos.order", payload["event_id"], payload)
    if ingest.duplicate:
        return

    try:
        with session_scope() as session:
            handle_pos_order(payload, session)
        if ingest.event_id:
            mark_processed(ingest.event_id)
        result.orders_ingested += 1
    except Exception as exc:
        result.orders_rejected += 1
        if ingest.event_id:
            mark_processed(ingest.event_id, error=str(exc))
        emit(
            StepResult(
                at=order.placed_at,
                label=f"Order {order.external_id}",
                kind="trading",
                status="failed",
                detail=f"{type(exc).__name__}: {exc}",
            )
        )


def _run_agent_step(
    agent_name: str,
    label: str,
    at: datetime,
    business_date: date,
    auto_approve: bool,
    result: SimulationResult,
    emit,
) -> None:
    from restaurant_ai.approvals.service import resolve
    from restaurant_ai.db.models import ApprovalRequest, ApprovalStatus
    from restaurant_ai.kernel.registry import get_agent
    from restaurant_ai.kernel.runner import run_agent

    payload = (
        # build_week defaults to next week; the simulated day needs today's own
        # roster, or there are no shifts to record hours against.
        {"week_starting": business_date.isoformat(), "days": 1}
        if agent_name == "shift_scheduling"
        else None
    )
    try:
        outcome = run_agent(
            get_agent(agent_name),
            business_date=business_date,
            trigger="simulation",
            trigger_ref=label,
            trigger_payload=payload,
        )
    except Exception as exc:
        emit(
            StepResult(
                at=at,
                label=f"{label} ({agent_name})",
                kind="agent",
                status="failed",
                detail=f"{type(exc).__name__}: {exc}",
            )
        )
        return

    if outcome.error:
        emit(
            StepResult(
                at=at,
                label=f"{label} ({agent_name})",
                kind="agent",
                status="failed",
                detail=outcome.error,
            )
        )
        return

    if not outcome.interrupted:
        emit(
            StepResult(
                at=at,
                label=f"{label} ({agent_name})",
                kind="agent",
                status="ok",
                detail=outcome.summary,
                payload=_trim(outcome.results),
            )
        )
        return

    # Parked for approval.
    result.approvals_requested += 1
    proposal = (outcome.interrupt_payload or {}).get("proposals", [{}])[0]
    emit(
        StepResult(
            at=at,
            label=f"{label} ({agent_name})",
            kind="approval",
            status="awaiting_approval",
            detail=proposal.get("summary", ""),
            payload={"value": proposal.get("value")},
        )
    )

    if not auto_approve:
        return

    from sqlalchemy import select

    with session_scope() as session:
        pending = list(
            session.execute(
                select(ApprovalRequest).where(
                    ApprovalRequest.run_id == outcome.run_id,
                    ApprovalRequest.status == ApprovalStatus.PENDING,
                )
            ).scalars()
        )
        ids = [r.id for r in pending]

    for approval_id in ids:
        resolved = resolve(
            approval_id, approved=True, resolved_by="simulation", note="auto-approved"
        )
        result.approvals_approved += 1
        emit(
            StepResult(
                at=at,
                label=f"{label} approved",
                kind="approval",
                status="approved",
                detail=resolved.get("summary", ""),
            )
        )


def _record_hours(business_date: date, at: datetime, emit) -> None:
    """Pull actual hours from payroll and write the time entries.

    Labour is half of prime cost. Without this the end-of-day report would show
    a flatteringly low prime cost that simply omits everyone's wages.
    """
    from sqlalchemy import delete as sa_delete
    from sqlalchemy import select

    from restaurant_ai.db.models import Staff, TimeEntry
    from restaurant_ai.integrations import get_integrations

    hours = get_integrations().payroll.fetch_hours(business_date)
    if not hours:
        emit(
            StepResult(
                at=at,
                label="Record hours worked",
                kind="agent",
                status="ok",
                detail="No roster published for today, so no hours to record.",
            )
        )
        return

    total = ZERO
    with session_scope() as session:
        session.execute(sa_delete(TimeEntry).where(TimeEntry.business_date == business_date))
        codes = {s.employee_code: s for s in session.execute(select(Staff)).scalars()}
        for entry in hours:
            staff = codes.get(entry.employee_code)
            if staff is None:
                continue
            # Clock times derived from the hours actually worked, so the entry
            # is internally consistent: a flat 10:00-23:00 against a 6-hour cost
            # is contradictory data that would mislead anyone reading it.
            clock_in = datetime.combine(business_date, time(10, 0), tzinfo=clock.local_tz())
            session.add(
                TimeEntry(
                    staff_id=staff.id,
                    business_date=business_date,
                    clock_in=clock_in,
                    clock_out=clock_in + timedelta(hours=float(entry.hours)),
                    hourly_rate=entry.hourly_rate,
                    cost=entry.cost,
                )
            )
            total += entry.cost

    emit(
        StepResult(
            at=at,
            label="Record hours worked",
            kind="agent",
            status="ok",
            detail=f"{len(hours)} shift(s) worked, labour cost {total:.2f}.",
        )
    )


def _load_report(business_date: date) -> dict[str, Any]:
    from sqlalchemy import select

    from restaurant_ai.db.models import DailyReport, ReconciliationBatch

    with session_scope() as session:
        report = session.execute(
            select(DailyReport).where(DailyReport.business_date == business_date)
        ).scalar_one_or_none()
        batch = session.execute(
            select(ReconciliationBatch).where(ReconciliationBatch.business_date == business_date)
        ).scalar_one_or_none()

        if report is None:
            return {}
        return {
            "business_date": business_date.isoformat(),
            "net_revenue": str(report.net_revenue),
            "covers": report.covers,
            "average_check": str(report.average_check),
            "cogs": str(report.cogs),
            "food_cost_pct": str(report.food_cost_pct),
            "labour_cost": str(report.labour_cost),
            "labour_pct": str(report.labour_pct),
            "prime_cost": str(report.prime_cost),
            "prime_cost_pct": str(report.prime_cost_pct),
            "operating_margin_pct": str(report.operating_margin_pct),
            "commentary": report.commentary,
            "reconciliation": (
                {
                    "balanced": batch.is_balanced,
                    "variance": str(batch.variance),
                    "matched": batch.matched_count,
                    "exceptions": batch.unmatched_count,
                }
                if batch
                else None
            ),
        }


def journals_balance(business_date: date) -> tuple[bool, dict[str, Any]]:
    """Check every journal posted for the day balances.

    This is the assertion that matters most in the end-to-end test: if debits
    stop equalling credits, the books are wrong however good the report looks.
    """
    from sqlalchemy import func, select

    from restaurant_ai.db.models import JournalEntry, JournalLine

    with session_scope() as session:
        rows = session.execute(
            select(
                JournalEntry.entry_number,
                func.sum(JournalLine.debit),
                func.sum(JournalLine.credit),
            )
            .join(JournalLine, JournalLine.entry_id == JournalEntry.id)
            .where(JournalEntry.business_date == business_date)
            .group_by(JournalEntry.entry_number)
        ).all()

    details = {
        number: {"debits": str(debits), "credits": str(credits), "balanced": debits == credits}
        for number, debits, credits in rows
    }
    return (all(d["balanced"] for d in details.values()) if details else False), details


def _trim(results: dict[str, Any]) -> dict[str, Any]:
    """Keep step payloads small enough to print."""
    trimmed: dict[str, Any] = {}
    for tool, value in results.items():
        if isinstance(value, dict):
            trimmed[tool] = {
                k: v
                for k, v in value.items()
                if not isinstance(v, (list, dict)) and len(str(v)) < 120
            }
    return trimmed
