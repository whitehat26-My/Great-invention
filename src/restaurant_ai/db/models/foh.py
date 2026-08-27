"""Tables, reservations and seating."""

from __future__ import annotations

from datetime import date, datetime
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
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from restaurant_ai.db.base import Base, Money, Timestamped, UUIDPk
from restaurant_ai.db.models.enums import ReservationStatus


class TableDef(UUIDPk, Timestamped, Base):
    __tablename__ = "table_def"

    label: Mapped[str] = mapped_column(String(20), unique=True)
    seats: Mapped[int] = mapped_column(Integer)
    min_party: Mapped[int] = mapped_column(Integer, default=1)
    section: Mapped[str] = mapped_column(String(40), default="main")
    is_combinable: Mapped[bool] = mapped_column(Boolean, default=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    __table_args__ = (CheckConstraint("seats > 0", name="seats_positive"),)


class Reservation(UUIDPk, Timestamped, Base):
    __tablename__ = "reservation"

    reference: Mapped[str] = mapped_column(String(20), unique=True, index=True)
    guest_id: Mapped[str | None] = mapped_column(ForeignKey("guest.id"), index=True)
    guest_name: Mapped[str] = mapped_column(String(160))
    guest_phone: Mapped[str | None] = mapped_column(String(40))
    party_size: Mapped[int] = mapped_column(Integer)
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    # Expected release time = starts_at + turn time for this party size.
    expected_end_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    table_id: Mapped[str | None] = mapped_column(ForeignKey("table_def.id"), index=True)
    status: Mapped[ReservationStatus] = mapped_column(
        Enum(ReservationStatus, native_enum=False, length=20),
        default=ReservationStatus.REQUESTED,
        index=True,
    )
    source: Mapped[str] = mapped_column(String(30), default="whatsapp")
    special_requests: Mapped[str | None] = mapped_column(Text)
    business_date: Mapped[date] = mapped_column(Date, index=True)

    table: Mapped[TableDef | None] = relationship()
    guest: Mapped[object | None] = relationship("Guest")

    __table_args__ = (
        CheckConstraint("party_size > 0", name="party_size_positive"),
        Index("ix_reservation_table_window", "table_id", "starts_at", "expected_end_at"),
    )


class SeatingEvent(UUIDPk, Base):
    """Actual seat/clear times, which is how real turn times get measured."""

    __tablename__ = "seating_event"

    table_id: Mapped[str] = mapped_column(ForeignKey("table_def.id"), index=True)
    reservation_id: Mapped[str | None] = mapped_column(ForeignKey("reservation.id"))
    order_id: Mapped[str | None] = mapped_column(ForeignKey("order_header.id"))
    party_size: Mapped[int] = mapped_column(Integer)
    seated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    cleared_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    business_date: Mapped[date] = mapped_column(Date, index=True)
    spend: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"))

    table: Mapped[TableDef] = relationship()

    @property
    def turn_minutes(self) -> int | None:
        if self.cleared_at is None:
            return None
        return int((self.cleared_at - self.seated_at).total_seconds() // 60)
