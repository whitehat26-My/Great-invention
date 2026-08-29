"""The agent control plane: audit trail, approvals and event ingestion.

Every agent execution writes an ``agent_run`` and one ``agent_action`` per tool
call, which is what makes an autonomous system auditable after the fact: for any
purchase order or price change you can point at the run, the reasoning and the
human who approved it.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from restaurant_ai.db.base import Base, Money, Timestamped, UUIDPk
from restaurant_ai.db.models.enums import AgentRunStatus, ApprovalStatus


class AgentRun(UUIDPk, Timestamped, Base):
    __tablename__ = "agent_run"

    agent_name: Mapped[str] = mapped_column(String(60), index=True)
    department: Mapped[str] = mapped_column(String(40), index=True)
    trigger: Mapped[str] = mapped_column(String(60), doc="schedule | event | manual | api")
    trigger_ref: Mapped[str | None] = mapped_column(String(80))
    status: Mapped[AgentRunStatus] = mapped_column(
        Enum(AgentRunStatus, native_enum=False, length=24),
        default=AgentRunStatus.RUNNING,
        index=True,
    )
    business_date: Mapped[date] = mapped_column(Date, index=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # LangGraph checkpoint thread; how an interrupted run is resumed later.
    thread_id: Mapped[str] = mapped_column(String(80), unique=True, index=True)
    model: Mapped[str | None] = mapped_column(String(60))
    summary: Mapped[str | None] = mapped_column(Text, doc="Agent's own account of what it did.")
    error: Mapped[str | None] = mapped_column(Text)
    context: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    tokens_used: Mapped[int] = mapped_column(Integer, default=0)

    actions: Mapped[list[AgentAction]] = relationship(
        back_populates="run", cascade="all, delete-orphan", order_by="AgentAction.sequence"
    )
    approvals: Mapped[list[ApprovalRequest]] = relationship(back_populates="run")

    __table_args__ = (Index("ix_agent_run_agent_date", "agent_name", "business_date"),)


class AgentAction(UUIDPk, Base):
    """One tool invocation within a run."""

    __tablename__ = "agent_action"

    run_id: Mapped[str] = mapped_column(ForeignKey("agent_run.id"), index=True)
    sequence: Mapped[int] = mapped_column(Integer, default=0)
    tool_name: Mapped[str] = mapped_column(String(80), index=True)
    arguments: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    result: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    is_proposal: Mapped[bool] = mapped_column(
        Boolean, default=False, doc="True when the tool was gated and returned a proposal."
    )
    committed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error: Mapped[str | None] = mapped_column(Text)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    run: Mapped[AgentRun] = relationship(back_populates="actions")


class ApprovalRequest(UUIDPk, Timestamped, Base):
    """A gated action parked waiting for a human.

    ``thread_id`` is what lets a different process (the Slack webhook handler)
    resume the paused graph hours later.
    """

    __tablename__ = "approval_request"

    run_id: Mapped[str] = mapped_column(ForeignKey("agent_run.id"), index=True)
    thread_id: Mapped[str] = mapped_column(String(80), index=True)
    agent_name: Mapped[str] = mapped_column(String(60), index=True)
    title: Mapped[str] = mapped_column(String(200))
    detail: Mapped[str] = mapped_column(Text)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    value: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"))
    status: Mapped[ApprovalStatus] = mapped_column(
        Enum(ApprovalStatus, native_enum=False, length=16),
        default=ApprovalStatus.PENDING,
        index=True,
    )
    channel: Mapped[str] = mapped_column(String(20), default="none")
    channel_message_ref: Mapped[str | None] = mapped_column(String(120))
    requested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    resolved_by: Mapped[str | None] = mapped_column(String(120))
    resolution_note: Mapped[str | None] = mapped_column(Text)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    run: Mapped[AgentRun] = relationship(back_populates="approvals")


class InboundEvent(UUIDPk, Base):
    """Raw webhook payload, deduplicated on the provider's own event id.

    Written before any processing so a replayed webhook is a no-op and a failed
    handler can be retried from the stored body.
    """

    __tablename__ = "inbound_event"

    provider: Mapped[str] = mapped_column(String(40), index=True)
    event_type: Mapped[str] = mapped_column(String(60), index=True)
    external_id: Mapped[str] = mapped_column(String(120))
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    processing_error: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        UniqueConstraint("provider", "external_id", name="uq_inbound_event_provider_external"),
    )


class OutboxEvent(UUIDPk, Base):
    """Domain events published by agents, written in the same transaction as the
    state change so a crash cannot lose them (transactional outbox)."""

    __tablename__ = "outbox_event"

    topic: Mapped[str] = mapped_column(String(60), index=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    dispatched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str | None] = mapped_column(Text)
    source_run_id: Mapped[str | None] = mapped_column(String(36), index=True)


class ConversationTurn(UUIDPk, Base):
    """One thing said in the approvals chat, by the owner or by Keanu.

    Without this every message was answered alone, and the desk could not follow
    a conversation: "how much chicken is left?" worked, and "and rice?" meant
    nothing at all. That is not a small gap in politeness — it is the difference
    between a search box and someone to talk to.

    It lives in the database rather than in the listener's memory because the
    listener is restarted: by the supervisor when it dies, by a reboot, by an
    upgrade. A thread that survives the question and not the restart is worse
    than none, because it is unpredictable.

    Kept per chat, and read back within a time window — an exchange resumed
    hours later is a new conversation, and dragging this morning's stock
    question into tonight's roster question helps nobody.
    """

    __tablename__ = "conversation_turn"

    chat_id: Mapped[str] = mapped_column(String(40), index=True)
    role: Mapped[str] = mapped_column(String(10), doc="owner | keanu")
    text: Mapped[str] = mapped_column(Text)
    said_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)

    __table_args__ = (Index("ix_conversation_chat_time", "chat_id", "said_at"),)


class Reminder(UUIDPk, Timestamped, Base):
    """Something the owner has to do, and the date it stops being optional.

    A restaurant that has traded twenty years runs on things nobody wrote down:
    the halal certificate expires in March, the fire extinguisher service is due,
    the landlord wants the lease answer by Friday. They are remembered until the
    week they are not, and the cost of forgetting one is never small — a lapsed
    licence closes the door.

    This is the one place the platform holds work that is *the owner's* rather
    than an agent's. Everything else here is something an agent does; this is
    something Aziera makes sure a person does.

    ``done_at`` rather than a flag, because when it was dealt with is the
    question asked afterwards — by an inspector, or by the owner wondering
    whether they renewed it last year.
    """

    __tablename__ = "reminder"

    what: Mapped[str] = mapped_column(String(300))
    due_on: Mapped[date] = mapped_column(Date, index=True)
    # Free text: "the halal cert takes three weeks, start early".
    detail: Mapped[str | None] = mapped_column(Text)
    # Who asked for it — the owner, or an agent that noticed something.
    raised_by: Mapped[str] = mapped_column(String(60), default="owner")
    done_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # When the owner was last told about it, so a daily chase does not become
    # a daily identical message that gets learned and ignored.
    last_raised_on: Mapped[date | None] = mapped_column(Date)

    __table_args__ = (Index("ix_reminder_open", "due_on", "done_at"),)
