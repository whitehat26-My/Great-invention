"""Orders, payments and delivery-platform payouts.

This is what the POS webhook writes into, and it is the input to stock
deduction, menu engineering, forecasting and the daily reconciliation.
"""

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

from restaurant_ai.db.base import Base, Money, Qty, Timestamped, UUIDPk
from restaurant_ai.db.models.enums import OrderChannel, OrderStatus, PaymentMethod


class Guest(UUIDPk, Timestamped, Base):
    """A known diner. Drives re-engagement segments and personalised replies."""

    __tablename__ = "guest"

    name: Mapped[str | None] = mapped_column(String(160))
    phone: Mapped[str | None] = mapped_column(String(40), unique=True)
    email: Mapped[str | None] = mapped_column(String(160))
    first_visit_on: Mapped[date | None] = mapped_column(Date)
    last_visit_on: Mapped[date | None] = mapped_column(Date, index=True)
    visit_count: Mapped[int] = mapped_column(Integer, default=0)
    lifetime_value: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"))
    dietary_notes: Mapped[str | None] = mapped_column(Text)
    marketing_opt_in: Mapped[bool] = mapped_column(Boolean, default=True)


class OrderHeader(UUIDPk, Timestamped, Base):
    __tablename__ = "order_header"

    order_number: Mapped[str] = mapped_column(String(40), unique=True, index=True)
    channel: Mapped[OrderChannel] = mapped_column(
        Enum(OrderChannel, native_enum=False, length=20), index=True
    )
    status: Mapped[OrderStatus] = mapped_column(
        Enum(OrderStatus, native_enum=False, length=20), default=OrderStatus.OPEN, index=True
    )
    table_id: Mapped[str | None] = mapped_column(ForeignKey("table_def.id"))
    guest_id: Mapped[str | None] = mapped_column(ForeignKey("guest.id"), index=True)
    party_size: Mapped[int] = mapped_column(Integer, default=1)
    placed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    business_date: Mapped[date] = mapped_column(Date, index=True)
    subtotal: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"))
    discount: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"))
    tax: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"))
    total: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"))
    # Set for delivery orders; commission is netted off in reconciliation.
    delivery_platform: Mapped[str | None] = mapped_column(String(40), index=True)
    external_ref: Mapped[str | None] = mapped_column(String(80), doc="POS/platform order id.")
    notes: Mapped[str | None] = mapped_column(Text)

    lines: Mapped[list[OrderLine]] = relationship(
        back_populates="order", cascade="all, delete-orphan"
    )
    payments: Mapped[list[Payment]] = relationship(
        back_populates="order", cascade="all, delete-orphan"
    )
    guest: Mapped[Guest | None] = relationship()

    __table_args__ = (Index("ix_order_header_date_channel", "business_date", "channel"),)


class OrderLine(UUIDPk, Base):
    __tablename__ = "order_line"

    order_id: Mapped[str] = mapped_column(ForeignKey("order_header.id"), index=True)
    menu_item_id: Mapped[str] = mapped_column(ForeignKey("menu_item.id"), index=True)
    quantity: Mapped[Decimal] = mapped_column(Qty, default=Decimal("1"))
    unit_price: Mapped[Decimal] = mapped_column(Money)
    line_total: Mapped[Decimal] = mapped_column(Money)
    # Free-text customisation from the conversational order agent
    # ("no peanuts", "extra spicy"). Kept verbatim for the kitchen ticket.
    modifiers: Mapped[str | None] = mapped_column(Text)
    course: Mapped[int] = mapped_column(Integer, default=2)
    is_voided: Mapped[bool] = mapped_column(Boolean, default=False)
    # Set once the stock agent has deducted this line's BOM, so replays are safe.
    stock_deducted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    order: Mapped[OrderHeader] = relationship(back_populates="lines")
    menu_item: Mapped[object] = relationship("MenuItem")

    __table_args__ = (CheckConstraint("quantity > 0", name="quantity_positive"),)


class Payment(UUIDPk, Base):
    __tablename__ = "payment"

    order_id: Mapped[str] = mapped_column(ForeignKey("order_header.id"), index=True)
    method: Mapped[PaymentMethod] = mapped_column(
        Enum(PaymentMethod, native_enum=False, length=24), index=True
    )
    amount: Mapped[Decimal] = mapped_column(Money)
    tip: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"))
    paid_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    business_date: Mapped[date] = mapped_column(Date, index=True)
    # Processor reference, used to match against the merchant statement.
    processor_ref: Mapped[str | None] = mapped_column(String(80), index=True)
    is_reconciled: Mapped[bool] = mapped_column(Boolean, default=False, index=True)

    order: Mapped[OrderHeader] = relationship(back_populates="payments")


class DeliveryPayout(UUIDPk, Timestamped, Base):
    """A platform's settlement: gross sales less commission, paid in arrears."""

    __tablename__ = "delivery_payout"

    platform: Mapped[str] = mapped_column(String(40), index=True)
    payout_ref: Mapped[str] = mapped_column(String(80), unique=True)
    period_start: Mapped[date] = mapped_column(Date)
    period_end: Mapped[date] = mapped_column(Date)
    gross_sales: Mapped[Decimal] = mapped_column(Money)
    commission: Mapped[Decimal] = mapped_column(Money)
    adjustments: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"))
    net_payout: Mapped[Decimal] = mapped_column(Money)
    received_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    is_reconciled: Mapped[bool] = mapped_column(Boolean, default=False, index=True)


class BankTransaction(UUIDPk, Timestamped, Base):
    """A line from the merchant/bank statement, to be matched to takings."""

    __tablename__ = "bank_transaction"

    statement_ref: Mapped[str] = mapped_column(String(80), unique=True)
    posted_on: Mapped[date] = mapped_column(Date, index=True)
    description: Mapped[str] = mapped_column(String(240))
    amount: Mapped[Decimal] = mapped_column(Money, doc="Signed: credits positive.")
    counterparty: Mapped[str | None] = mapped_column(String(160))
    is_reconciled: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
