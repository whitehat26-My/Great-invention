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
