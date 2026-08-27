"""Double-entry ledger and daily reconciliation.

Journals are balanced at write time by ``domain.reconciliation.post_entry``;
the CHECK constraint here is the belt-and-braces backstop. Nothing in the
platform mutates a posted journal — corrections are new, reversing entries.
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

from restaurant_ai.db.base import Base, Money, Timestamped, UUIDPk
from restaurant_ai.db.models.enums import AccountType


class LedgerAccount(UUIDPk, Timestamped, Base):
    __tablename__ = "ledger_account"

    code: Mapped[str] = mapped_column(String(20), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(120))
    type: Mapped[AccountType] = mapped_column(Enum(AccountType, native_enum=False, length=16))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    @property
    def is_debit_normal(self) -> bool:
        return self.type in (AccountType.ASSET, AccountType.EXPENSE)


class JournalEntry(UUIDPk, Timestamped, Base):
    __tablename__ = "journal_entry"

    entry_number: Mapped[str] = mapped_column(String(40), unique=True, index=True)
    business_date: Mapped[date] = mapped_column(Date, index=True)
    posted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    memo: Mapped[str] = mapped_column(Text)
    source: Mapped[str] = mapped_column(String(60), doc="Which agent or process posted it.")
    source_id: Mapped[str | None] = mapped_column(String(64), index=True)
    is_reversal_of: Mapped[str | None] = mapped_column(ForeignKey("journal_entry.id"))

    lines: Mapped[list[JournalLine]] = relationship(
        back_populates="entry", cascade="all, delete-orphan"
    )

    @property
    def total_debit(self) -> Decimal:
        return sum((line.debit for line in self.lines), Decimal("0"))

    @property
    def total_credit(self) -> Decimal:
        return sum((line.credit for line in self.lines), Decimal("0"))

    @property
    def is_balanced(self) -> bool:
        return self.total_debit == self.total_credit


class JournalLine(UUIDPk, Base):
    __tablename__ = "journal_line"

    entry_id: Mapped[str] = mapped_column(ForeignKey("journal_entry.id"), index=True)
    account_id: Mapped[str] = mapped_column(ForeignKey("ledger_account.id"), index=True)
    debit: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"))
    credit: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"))
    memo: Mapped[str | None] = mapped_column(String(240))

    entry: Mapped[JournalEntry] = relationship(back_populates="lines")
    account: Mapped[LedgerAccount] = relationship()

    __table_args__ = (
        # A line is one side or the other, never both, never neither.
        CheckConstraint("(debit = 0) <> (credit = 0)", name="one_sided"),
        CheckConstraint("debit >= 0 AND credit >= 0", name="non_negative"),
        Index("ix_journal_line_account_entry", "account_id", "entry_id"),
    )


class ReconciliationBatch(UUIDPk, Timestamped, Base):
    """One day's reconciliation run and its outcome."""

    __tablename__ = "reconciliation_batch"

    business_date: Mapped[date] = mapped_column(Date, unique=True, index=True)
    run_id: Mapped[str | None] = mapped_column(String(36), index=True)
    pos_total: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"))
    card_settled: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"))
    cash_counted: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"))
    delivery_net: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"))
    variance: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"))
    matched_count: Mapped[int] = mapped_column(Integer, default=0)
    unmatched_count: Mapped[int] = mapped_column(Integer, default=0)
    is_balanced: Mapped[bool] = mapped_column(Boolean, default=False)
    notes: Mapped[str | None] = mapped_column(Text)

    matches: Mapped[list[ReconciliationMatch]] = relationship(
        back_populates="batch", cascade="all, delete-orphan"
    )


class ReconciliationMatch(UUIDPk, Base):
    """A matched (or exception) pairing between an internal record and a statement line."""

    __tablename__ = "reconciliation_match"

    batch_id: Mapped[str] = mapped_column(ForeignKey("reconciliation_batch.id"), index=True)
    left_type: Mapped[str] = mapped_column(String(40), doc="payment | delivery_payout")
    left_id: Mapped[str] = mapped_column(String(36))
    right_type: Mapped[str | None] = mapped_column(String(40), doc="bank_transaction")
    right_id: Mapped[str | None] = mapped_column(String(36))
    amount: Mapped[Decimal] = mapped_column(Money)
    difference: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"))
    is_exception: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    reason: Mapped[str | None] = mapped_column(String(240))

    batch: Mapped[ReconciliationBatch] = relationship(back_populates="matches")


class DailyReport(UUIDPk, Timestamped, Base):
    """The end-of-day performance snapshot produced by the Daily Performance agent."""

    __tablename__ = "daily_report"

    business_date: Mapped[date] = mapped_column(Date, unique=True, index=True)
    run_id: Mapped[str | None] = mapped_column(String(36))
    net_revenue: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"))
    covers: Mapped[int] = mapped_column(Integer, default=0)
    average_check: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"))
    cogs: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"))
    labour_cost: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"))
    prime_cost: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"))
    prime_cost_pct: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"))
    labour_pct: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"))
    food_cost_pct: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"))
    operating_margin_pct: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"))
    waste_cost: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"))
    commentary: Mapped[str | None] = mapped_column(Text, doc="LLM-written narrative.")
