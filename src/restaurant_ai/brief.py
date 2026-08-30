"""The owner's daily brief.

The agents write their work into the database and Camelia closes the day —
but the results were scattered across a CLI, a REST API and an approvals chat.
Nobody stitched them into the one thing an owner actually reads.

This is that thing: every department in one message, delivered to the same
Telegram chat where the approval cards already arrive. The phone is the UI.

Two decisions shape it:

- **It reports what the agents saw**, not a parallel reimplementation. The
  supply section is Rain's own ``perceive`` — the same reorder view Rain plans
  from — so the brief and the agents cannot disagree about the state of the
  restaurant.
- **A broken section is a line, not a crash.** The brief is often read at
  midnight after close; a division-by-zero in one department must not cost the
  owner the other five. Every section is built independently and degrades to
  "unavailable (reason)".
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from restaurant_ai import clock, demo
from restaurant_ai.config import get_settings
from restaurant_ai.db.models import (
    AgentRun,
    AgentRunStatus,
    DailyReport,
    KdsTicket,
    OrderHeader,
    PurchaseOrder,
    PurchaseOrderStatus,
    Review,
    Shift,
    SocialPost,
    TicketStatus,
)
from restaurant_ai.faults import short_fault
from restaurant_ai.kernel.registry import all_agents
from restaurant_ai.kernel.spec import ToolContext
from restaurant_ai.logging_setup import get_logger

log = get_logger(__name__)


@dataclass
class Brief:
    business_date: date
    sections: dict[str, list[str]] = field(default_factory=dict)
    needs_you: list[str] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)


def _section(brief: Brief, name: str, build: Callable[[], list[str]]) -> None:
    """One department, isolated. Its failure becomes its content."""
    try:
        brief.sections[name] = build()
    except Exception as exc:  # the other five sections still matter at midnight
        # The log gets the whole truth; the owner gets a sentence. A phone
        # bubble full of SELECT columns and a caret teaches nobody anything.
        log.warning("brief section failed", section=name, error=str(exc))
        brief.sections[name] = [f"unavailable — {short_fault(exc)}"]


def _perceive(session: Session, agent_name: str, business_date: date) -> dict[str, Any]:
    """An agent's own read-only view — the brief reports what the agents see."""
    spec = all_agents()[agent_name]
    if spec.perceive is None:
        return {}
    return (
        spec.perceive(
            ToolContext(
                session=session,
                run_id="daily-brief",
                agent_name=agent_name,
                business_date=business_date,
                state={},
            )
        )
        or {}
    )


def _pct(value: Any) -> str:
    return f"{Decimal(str(value)) * 100:.1f}%"


def build_brief(session: Session, business_date: date | None = None) -> Brief:
    day = business_date or clock.today()
    brief = Brief(business_date=day)

    # --- Money ---------------------------------------------------------------
    def money() -> list[str]:
        report = session.execute(
            select(DailyReport).where(DailyReport.business_date == day)
        ).scalar_one_or_none()
        if report is None:
            waiting = ["day not closed yet — Camelia reports after 23:45"]
            caveat = demo.describe(session)
            if caveat:
                waiting.append(f"({caveat})")
            return waiting
        lines = [
            f"revenue {report.net_revenue:,.2f} · {report.covers} covers "
            f"· avg check {report.average_check:,.2f}",
            f"COGS {_pct(report.food_cost_pct)} · labour {_pct(report.labour_pct)} "
            f"· prime {_pct(report.prime_cost_pct)} · margin {_pct(report.operating_margin_pct)}",
        ]
        if report.commentary:
            # Camelia's first sentence is the verdict; the rest is detail.
            first = report.commentary.split(". ")[0].strip()
            lines.append(first + ("" if first.endswith(".") else "."))
        # Said last, so it is the note the owner leaves the section with.
        caveat = demo.describe(session)
        if caveat:
            lines.append(f"({caveat})")
        return lines

    _section(brief, "money", money)

    # --- Supply --------------------------------------------------------------
    def supply() -> list[str]:
        seen = _perceive(session, "stock_reorder", day)
        below = seen.get("below_reorder_point", 0)
        lines = [
            f"{seen.get('ingredients_tracked', 0)} ingredients tracked, {below} at/below reorder point"
        ]
        for item in (seen.get("items") or [])[:3]:
            lines.append(
                f"  {item['ingredient']}: {item['days_cover']}d cover "
                f"(on hand {item['on_hand']}, on order {item['on_order']})"
            )
        open_pos = session.execute(
            select(func.count())
            .select_from(PurchaseOrder)
            .where(
                PurchaseOrder.status.in_(
                    [PurchaseOrderStatus.SENT, PurchaseOrderStatus.PARTIALLY_RECEIVED]
                )
            )
        ).scalar_one()
        if open_pos:
            lines.append(f"{open_pos} purchase order(s) out with suppliers")
        return lines

    _section(brief, "supply", supply)

    # --- Kitchen -------------------------------------------------------------
    def kitchen() -> list[str]:
        # Tickets carry no business_date of their own; the day lives on the
        # order they belong to.
        counts: dict[TicketStatus, int] = dict(
            session.execute(
                select(KdsTicket.status, func.count())
                .join(OrderHeader, KdsTicket.order_id == OrderHeader.id)
                .where(OrderHeader.business_date == day)
                .group_by(KdsTicket.status)
            )
            .tuples()
            .all()
        )
        total = sum(counts.values())
        if not total:
            return ["no tickets today"]
        in_flight = counts.get(TicketStatus.QUEUED, 0) + counts.get(TicketStatus.IN_PROGRESS, 0)
        return [
            f"{total} tickets · {counts.get(TicketStatus.SERVED, 0)} served "
            f"· {counts.get(TicketStatus.READY, 0)} at the pass · {in_flight} in flight"
        ]

    _section(brief, "kitchen", kitchen)

    # --- Floor ---------------------------------------------------------------
    def floor() -> list[str]:
        rows = session.execute(
            select(Review.rating, Review.is_escalated).where(Review.business_date == day)
        ).all()
        if not rows:
            return ["no new reviews today"]
        average = sum(r for r, _ in rows) / len(rows)
        escalated = sum(1 for _, e in rows if e)
        line = f"{len(rows)} review(s), averaging {average:.1f}★"
        if escalated:
            line += f" · {escalated} escalated to management"
        return [line]

    _section(brief, "floor", floor)

    # --- Marketing -----------------------------------------------------------
    def marketing() -> list[str]:
        drafted, published = 0, 0
        for (ref,) in session.execute(select(SocialPost.external_ref)).all():
            published += 1 if ref else 0
            drafted += 0 if ref else 1
        lines = [f"{published} post(s) live, {drafted} drafted awaiting approval"]
        return lines

    _section(brief, "marketing", marketing)

    # --- People --------------------------------------------------------------
    def people() -> list[str]:
        def hours_on(target: date) -> tuple[int, Decimal]:
            shifts = (
                session.execute(select(Shift).where(Shift.business_date == target)).scalars().all()
            )
            total = sum(
                (Decimal((s.ends_at - s.starts_at).total_seconds()) / 3600 for s in shifts),
                Decimal("0"),
            )
            return len(shifts), total.quantize(Decimal("0.1"))

        today_count, today_hours = hours_on(day)
        tomorrow_count, _ = hours_on(day + timedelta(days=1))
        lines = [f"{today_count} shift(s) today, {today_hours}h rostered"]
        lines.append(
            f"tomorrow: {tomorrow_count} shift(s) rostered"
            if tomorrow_count
            else "tomorrow: NO ROSTER YET — Henry publishes at 05:30"
        )
        return lines

    _section(brief, "people", people)

    # --- The agents themselves ----------------------------------------------
    def agents() -> list[str]:
        latest: dict[str, AgentRun] = {}
        for run in session.execute(
            select(AgentRun).where(AgentRun.business_date == day).order_by(AgentRun.started_at)
        ).scalars():
            latest[run.agent_name] = run  # last run of the day wins

        ran = len(latest)
        failed = [r for r in latest.values() if r.status == AgentRunStatus.FAILED]
        waiting = [r for r in latest.values() if r.status == AgentRunStatus.AWAITING_APPROVAL]
        line = f"{ran}/{len(all_agents())} agents ran today"
        if failed:
            line += f" · {len(failed)} FAILED"
        if waiting:
            line += f" · {len(waiting)} awaiting approval"
        for run in failed:
            person = all_agents()[run.agent_name].person or run.agent_name
            brief.failures.append(f"{person}: {(run.error or run.summary or 'failed')[:140]}")
        return [line]

    _section(brief, "agents", agents)

    # --- What needs the owner -------------------------------------------------
    try:
        from restaurant_ai.approvals.service import list_pending
        from restaurant_ai.kernel.registry import display_name

        for pending in list_pending():
            brief.needs_you.append(
                f"{display_name(str(pending['agent']))}: {pending['title']} "
                f"(value {pending['value']})"
            )
    except Exception as exc:
        brief.needs_you.append(f"could not list pending approvals ({exc})")

    # --- The owner's own diary -----------------------------------------------
    # An approval is work an agent has prepared and a person must decide. These
    # are the opposite: work only the owner can do, that no agent will ever pick
    # up. They belong in the same place, because the brief is the one thing that
    # reliably gets read — and a reminder nobody reads is not a reminder.
    try:
        from restaurant_ai import reminders

        for item in reminders.due(session, on=day):
            brief.needs_you.append(f"{'LATE — ' if item.overdue else ''}{item.phrase()}")
    except Exception as exc:
        brief.needs_you.append(f"could not read the diary ({exc})")

    return brief


# ---------------------------------------------------------------------------
# Rendering & delivery
# ---------------------------------------------------------------------------

_ORDER = [
    ("money", "MONEY — Emil & Camelia"),
    ("supply", "SUPPLY — Rain & Suri"),
    ("kitchen", "KITCHEN — Betrisha & Ciknor"),
    ("floor", "FLOOR — Aziera"),
    ("marketing", "MARKETING — Franky & Irma"),
    ("people", "PEOPLE — Henry & Kaksu"),
    ("agents", "AGENTS"),
]

# Telegram rejects messages over 4096 characters outright; leave headroom.
_TELEGRAM_LIMIT = 3800


def render_brief(brief: Brief) -> str:
    settings = get_settings()
    lines = [f"{settings.restaurant_name} — daily brief, {brief.business_date}"]
    for key, heading in _ORDER:
        content = brief.sections.get(key)
        if not content:
            continue
        lines.append("")
        lines.append(heading)
        lines.extend(f"  {line}" for line in content)

    if brief.failures:
        lines += ["", "FAILED TODAY"]
        lines.extend(f"  {line}" for line in brief.failures)

    lines.append("")
    if brief.needs_you:
        lines.append("NEEDS YOU")
        lines.extend(f"  - {line}" for line in brief.needs_you)
        lines.append("  (approve or reject on the cards above in this chat)")
    else:
        lines.append("Nothing needs you tonight.")

    text = "\n".join(lines)
    if len(text) > _TELEGRAM_LIMIT:
        text = text[:_TELEGRAM_LIMIT] + "\n… (truncated)"
    return text


def send_brief(text: str) -> bool:
    """Deliver to the approvals chat. Returns False, loudly, when it cannot."""
    from restaurant_ai.approvals.telegram import api

    settings = get_settings()
    if not settings.telegram_bot_token or not settings.telegram_chat_id:
        log.warning("brief not sent: telegram is not configured")
        return False
    api("sendMessage", chat_id=settings.telegram_chat_id, text=text)
    return True
