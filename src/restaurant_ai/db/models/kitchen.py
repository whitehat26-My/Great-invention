"""Prep plans, KDS tickets and forecast accuracy."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
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
from sqlalchemy.orm import Mapped, mapped_column, relationship

from restaurant_ai.db.base import Base, Money, Qty, Timestamped, UUIDPk
from restaurant_ai.db.models.enums import Station, TicketStatus


class PrepPlan(UUIDPk, Timestamped, Base):
    """The morning prep list for one service day."""

    __tablename__ = "prep_plan"

    business_date: Mapped[date] = mapped_column(Date, unique=True, index=True)
    run_id: Mapped[str | None] = mapped_column(String(36))
    forecast_covers: Mapped[int] = mapped_column(Integer, default=0)
    forecast_revenue: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"))
    notes: Mapped[str | None] = mapped_column(Text)

    lines: Mapped[list[PrepPlanLine]] = relationship(
        back_populates="plan", cascade="all, delete-orphan"
    )


class PrepPlanLine(UUIDPk, Base):
    """How much of one ingredient to prep, grossed up for yield loss."""

    __tablename__ = "prep_plan_line"

    plan_id: Mapped[str] = mapped_column(ForeignKey("prep_plan.id"), index=True)
    ingredient_id: Mapped[str] = mapped_column(ForeignKey("ingredient.id"), index=True)
    forecast_usage: Mapped[Decimal] = mapped_column(Qty, doc="Net requirement, base units.")
    prep_quantity: Mapped[Decimal] = mapped_column(Qty, doc="Grossed up by yield_pct.")
    uom: Mapped[str] = mapped_column(String(12))
    on_hand_at_plan: Mapped[Decimal] = mapped_column(Qty, default=Decimal("0"))
    actual_usage: Mapped[Decimal | None] = mapped_column(Qty, doc="Backfilled at EOD.")

    plan: Mapped[PrepPlan] = relationship(back_populates="lines")


class ItemForecast(UUIDPk, Base):
    """Per-item demand forecast, kept so accuracy can be scored the next day."""

    __tablename__ = "item_forecast"

    business_date: Mapped[date] = mapped_column(Date, index=True)
    menu_item_id: Mapped[str] = mapped_column(ForeignKey("menu_item.id"), index=True)
    forecast_qty: Mapped[Decimal] = mapped_column(Qty)
    actual_qty: Mapped[Decimal | None] = mapped_column(Qty)
    abs_error: Mapped[Decimal | None] = mapped_column(Qty)
    method: Mapped[str] = mapped_column(String(40), default="seasonal_naive")

    __table_args__ = (
        UniqueConstraint("business_date", "menu_item_id", name="uq_item_forecast_date_item"),
    )


class KdsTicket(UUIDPk, Timestamped, Base):
    """A station's work item, sequenced by the pacing agent."""

    __tablename__ = "kds_ticket"

    order_id: Mapped[str] = mapped_column(ForeignKey("order_header.id"), index=True)
    order_line_id: Mapped[str] = mapped_column(ForeignKey("order_line.id"), index=True)
    station: Mapped[Station] = mapped_column(
        Enum(Station, native_enum=False, length=20), index=True
    )
    status: Mapped[TicketStatus] = mapped_column(
        Enum(TicketStatus, native_enum=False, length=20), default=TicketStatus.QUEUED, index=True
    )
    course: Mapped[int] = mapped_column(Integer, default=2)
    sequence: Mapped[int] = mapped_column(Integer, default=0, doc="Fire order within the station.")
    fire_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    estimated_seconds: Mapped[int] = mapped_column(Integer, default=300)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    modifiers: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (Index("ix_kds_ticket_station_fire", "station", "fire_at"),)
