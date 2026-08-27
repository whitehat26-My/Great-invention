"""Enumerations shared by the ORM models.

Stored as VARCHAR with a CHECK constraint (``native_enum=False``) rather than
PostgreSQL ENUM types, because altering a native enum in a migration is painful
and these lists will grow.
"""

from __future__ import annotations

from enum import StrEnum


class OrderChannel(StrEnum):
    DINE_IN = "dine_in"
    TAKEAWAY = "takeaway"
    DRIVE_THRU = "drive_thru"
    DELIVERY = "delivery"
    KIOSK = "kiosk"
    PHONE = "phone"


class OrderStatus(StrEnum):
    OPEN = "open"
    FIRED = "fired"
    SERVED = "served"
    CLOSED = "closed"
    VOID = "void"


class PaymentMethod(StrEnum):
    CASH = "cash"
    CARD = "card"
    EWALLET = "ewallet"
    DELIVERY_PLATFORM = "delivery_platform"
    VOUCHER = "voucher"


class MovementReason(StrEnum):
    SALE = "sale"
    RECEIPT = "receipt"
    WASTE = "waste"
    COUNT_ADJUSTMENT = "count_adjustment"
    TRANSFER = "transfer"
    PREP = "prep"


class PurchaseOrderStatus(StrEnum):
    DRAFT = "draft"
    PENDING_APPROVAL = "pending_approval"
    APPROVED = "approved"
    SENT = "sent"
    PARTIALLY_RECEIVED = "partially_received"
    RECEIVED = "received"
    CANCELLED = "cancelled"
    REJECTED = "rejected"


class InvoiceStatus(StrEnum):
    RECEIVED = "received"
    MATCHED = "matched"
    DISPUTED = "disputed"
    APPROVED_FOR_PAYMENT = "approved_for_payment"
    PAID = "paid"


class DiscrepancyKind(StrEnum):
    PRICE = "price"
    QUANTITY = "quantity"
    TAX = "tax"
    MISSING_LINE = "missing_line"
    UNEXPECTED_LINE = "unexpected_line"


class ReservationStatus(StrEnum):
    REQUESTED = "requested"
    CONFIRMED = "confirmed"
    SEATED = "seated"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    NO_SHOW = "no_show"


class Station(StrEnum):
    GRILL = "grill"
    FRY = "fry"
    SAUTE = "saute"
    COLD = "cold"
    PASTRY = "pastry"
    BAR = "bar"
    EXPO = "expo"


class TicketStatus(StrEnum):
    QUEUED = "queued"
    IN_PROGRESS = "in_progress"
    READY = "ready"
    SERVED = "served"


class ShiftRole(StrEnum):
    CHEF = "chef"
    LINE_COOK = "line_cook"
    KITCHEN_PORTER = "kitchen_porter"
    SERVER = "server"
    HOST = "host"
    BARISTA = "barista"
    MANAGER = "manager"


class AgentRunStatus(StrEnum):
    RUNNING = "running"
    AWAITING_APPROVAL = "awaiting_approval"
    COMPLETED = "completed"
    FAILED = "failed"
    REJECTED = "rejected"


class ApprovalStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"


class AccountType(StrEnum):
    ASSET = "asset"
    LIABILITY = "liability"
    EQUITY = "equity"
    REVENUE = "revenue"
    EXPENSE = "expense"


class MenuClass(StrEnum):
    """Menu-engineering quadrants: popularity x contribution margin."""

    STAR = "star"  # high popularity, high margin
    PLOWHORSE = "plowhorse"  # high popularity, low margin
    PUZZLE = "puzzle"  # low popularity, high margin
    DOG = "dog"  # low popularity, low margin


class ReviewSentiment(StrEnum):
    POSITIVE = "positive"
    NEUTRAL = "neutral"
    NEGATIVE = "negative"
