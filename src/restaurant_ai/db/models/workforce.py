"""Staff, availability, rosters and the SOP knowledge base."""

from __future__ import annotations

from datetime import date, datetime, time
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    Time,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from restaurant_ai.db.base import Base, Money, Timestamped, UUIDPk
from restaurant_ai.db.models.enums import ShiftRole


class Staff(UUIDPk, Timestamped, Base):
    __tablename__ = "staff"

    employee_code: Mapped[str] = mapped_column(String(30), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(160))
    role: Mapped[ShiftRole] = mapped_column(
        Enum(ShiftRole, native_enum=False, length=24), index=True
    )
    hourly_rate: Mapped[Decimal] = mapped_column(Money)
    max_weekly_hours: Mapped[int] = mapped_column(Integer, default=44)
    min_rest_hours: Mapped[int] = mapped_column(Integer, default=11)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    phone: Mapped[str | None] = mapped_column(String(40))
    started_on: Mapped[date | None] = mapped_column(Date)

    availability: Mapped[list[Availability]] = relationship(
        back_populates="staff", cascade="all, delete-orphan"
    )

    __table_args__ = (CheckConstraint("hourly_rate >= 0", name="hourly_rate_non_negative"),)


class Availability(UUIDPk, Base):
    """Recurring weekly availability window."""

    __tablename__ = "availability"

    staff_id: Mapped[str] = mapped_column(ForeignKey("staff.id"), index=True)
    weekday: Mapped[int] = mapped_column(Integer, doc="0=Monday .. 6=Sunday")
    start_time: Mapped[time] = mapped_column(Time)
    end_time: Mapped[time] = mapped_column(Time)

    staff: Mapped[Staff] = relationship(back_populates="availability")

    __table_args__ = (
        CheckConstraint("weekday BETWEEN 0 AND 6", name="weekday_range"),
        CheckConstraint("end_time > start_time", name="window_ordered"),
        UniqueConstraint("staff_id", "weekday", "start_time", name="uq_availability_slot"),
    )


class Shift(UUIDPk, Timestamped, Base):
    """A required slot on the roster: role, window and how many people."""

    __tablename__ = "shift"

    business_date: Mapped[date] = mapped_column(Date, index=True)
    role: Mapped[ShiftRole] = mapped_column(Enum(ShiftRole, native_enum=False, length=24))
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    ends_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    required_headcount: Mapped[int] = mapped_column(Integer, default=1)
    run_id: Mapped[str | None] = mapped_column(String(36), index=True)

    assignments: Mapped[list[ShiftAssignment]] = relationship(
        back_populates="shift", cascade="all, delete-orphan"
    )

    __table_args__ = (
        CheckConstraint("ends_at > starts_at", name="shift_window_ordered"),
        Index("ix_shift_date_role", "business_date", "role"),
    )

    @property
    def hours(self) -> Decimal:
        return Decimal((self.ends_at - self.starts_at).total_seconds()) / Decimal(3600)


class ShiftAssignment(UUIDPk, Timestamped, Base):
    __tablename__ = "shift_assignment"

    shift_id: Mapped[str] = mapped_column(ForeignKey("shift.id"), index=True)
    staff_id: Mapped[str] = mapped_column(ForeignKey("staff.id"), index=True)
    is_confirmed: Mapped[bool] = mapped_column(Boolean, default=False)
    # Set when a swap has been requested and is awaiting manager approval.
    swap_requested_with: Mapped[str | None] = mapped_column(ForeignKey("staff.id"))
    estimated_cost: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"))

    shift: Mapped[Shift] = relationship(back_populates="assignments")
    staff: Mapped[Staff] = relationship(foreign_keys=[staff_id])

    __table_args__ = (UniqueConstraint("shift_id", "staff_id", name="uq_shift_assignment"),)


class TimeEntry(UUIDPk, Base):
    """Actual clock in/out, which is what labour cost is computed from."""

    __tablename__ = "time_entry"

    staff_id: Mapped[str] = mapped_column(ForeignKey("staff.id"), index=True)
    business_date: Mapped[date] = mapped_column(Date, index=True)
    clock_in: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    clock_out: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    hourly_rate: Mapped[Decimal] = mapped_column(Money)
    cost: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"))

    staff: Mapped[Staff] = relationship()


class SopDocument(UUIDPk, Timestamped, Base):
    """Standard operating procedures, searched with Postgres full-text.

    A ``tsvector`` GIN index is added in the migration; a vector store would be
    overkill for a corpus this size and adds an external dependency.
    """

    __tablename__ = "sop_document"

    slug: Mapped[str] = mapped_column(String(80), unique=True, index=True)
    title: Mapped[str] = mapped_column(String(200))
    category: Mapped[str] = mapped_column(String(60), index=True)
    body: Mapped[str] = mapped_column(Text)
    applies_to_role: Mapped[str | None] = mapped_column(String(24))
    version: Mapped[int] = mapped_column(Integer, default=1)
