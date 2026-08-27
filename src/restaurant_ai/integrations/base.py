"""Integration ports.

Every external system the restaurant touches is declared here as a Protocol and
implemented twice: once as a simulator that ships in the repo, and (eventually)
once against the real vendor. Agents depend on the Protocol, so swapping
GrabFood's real API in for the fake is one class and one environment variable,
and no agent changes.

The simulators are seeded deterministically. A given day always produces the
same orders, reviews and deliveries, which is what makes an end-to-end
simulated service day a usable regression test rather than a demo.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Protocol, runtime_checkable

# --- Transfer objects -------------------------------------------------------
# Deliberately vendor-neutral: each adapter maps its provider's payload into
# these, so nothing downstream knows whether the POS is Square or Toast.


@dataclass
class PosOrderLine:
    sku: str
    quantity: int
    unit_price: Decimal
    modifiers: str | None = None
    course: int = 2


@dataclass
class PosOrder:
    external_id: str
    channel: str
    placed_at: datetime
    lines: list[PosOrderLine]
    party_size: int = 1
    table_label: str | None = None
    guest_phone: str | None = None
    delivery_platform: str | None = None
    payment_method: str = "card"
    processor_ref: str | None = None
    notes: str | None = None

    @property
    def subtotal(self) -> Decimal:
        return sum((line.unit_price * line.quantity for line in self.lines), Decimal("0"))


@dataclass
class InboundMessage:
    external_id: str
    channel: str  # whatsapp | web | phone
    sender: str
    body: str
    received_at: datetime
    guest_name: str | None = None


@dataclass
class ReviewPost:
    external_id: str
    platform: str
    author: str
    rating: int
    body: str
    posted_at: datetime


@dataclass
class DeliveryNote:
    """What a supplier says they actually sent."""

    po_number: str
    delivered_at: datetime
    lines: list[tuple[str, Decimal]]  # (supplier_sku, packs delivered)
    note: str | None = None


@dataclass
class SupplierInvoiceDoc:
    """What the supplier billed. Deliberately allowed to disagree with the PO."""

    invoice_number: str
    supplier_code: str
    po_number: str | None
    invoice_date: date
    lines: list[tuple[str, Decimal, Decimal]]  # (supplier_sku, packs, unit price)
    tax: Decimal = Decimal("0")
    document_uri: str | None = None

    @property
    def subtotal(self) -> Decimal:
        return sum((packs * price for _, packs, price in self.lines), Decimal("0"))

    @property
    def total(self) -> Decimal:
        return self.subtotal + self.tax


@dataclass
class SettlementLine:
    """A line from the merchant statement or a platform payout."""

    reference: str
    amount: Decimal
    posted_on: date
    description: str
    counterparty: str | None = None


@dataclass
class PlatformPayout:
    platform: str
    payout_ref: str
    period_start: date
    period_end: date
    gross_sales: Decimal
    commission: Decimal
    adjustments: Decimal = Decimal("0")

    @property
    def net(self) -> Decimal:
        return self.gross_sales - self.commission + self.adjustments


@dataclass
class ScheduledPost:
    platform: str
    body: str
    scheduled_for: datetime
    external_ref: str | None = None
    media_uri: str | None = None


@dataclass
class StaffHours:
    employee_code: str
    business_date: date
    hours: Decimal
    hourly_rate: Decimal

    @property
    def cost(self) -> Decimal:
        return (self.hours * self.hourly_rate).quantize(Decimal("0.01"))


# --- Ports ------------------------------------------------------------------


@runtime_checkable
class POSPort(Protocol):
    """Point of sale. The source of every sale the platform reacts to."""

    def fetch_orders(self, since: datetime, until: datetime) -> list[PosOrder]: ...

    def push_order(self, order: PosOrder) -> str:
        """Send an order taken by the conversational agent into the POS."""
        ...


@runtime_checkable
class MessagingPort(Protocol):
    """WhatsApp/web/phone, for bookings and orders."""

    def fetch_messages(self, since: datetime) -> list[InboundMessage]: ...

    def send_message(self, recipient: str, body: str) -> str: ...


@runtime_checkable
class ReviewsPort(Protocol):
    """Google Reviews and social channels."""

    def fetch_reviews(self, since: datetime) -> list[ReviewPost]: ...

    def publish_response(self, review_external_id: str, body: str) -> str: ...


@runtime_checkable
class SupplierPort(Protocol):
    """Supplier ordering, delivery and invoicing."""

    def send_purchase_order(
        self,
        po_number: str,
        supplier_code: str,
        lines: list[tuple[str, Decimal]],
        unit_prices: dict[str, Decimal] | None = None,
    ) -> str:
        """Transmit a PO. ``unit_prices`` is the agreed price per supplier SKU,
        which the invoice three-way match later compares the bill against."""
        ...

    def fetch_deliveries(self, since: datetime) -> list[DeliveryNote]: ...

    def fetch_invoices(self, since: datetime) -> list[SupplierInvoiceDoc]: ...


@runtime_checkable
class SocialPort(Protocol):
    def schedule_post(self, post: ScheduledPost) -> str: ...

    def fetch_scheduled(self) -> list[ScheduledPost]: ...


@runtime_checkable
class PayrollPort(Protocol):
    def fetch_hours(self, business_date: date) -> list[StaffHours]: ...


@runtime_checkable
class BankPort(Protocol):
    """Merchant statements and delivery-platform payouts."""

    def fetch_settlements(self, business_date: date) -> list[SettlementLine]: ...

    def fetch_payouts(self, since: date) -> list[PlatformPayout]: ...


@dataclass
class Integrations:
    """The set of ports an agent can reach, resolved once per process."""

    pos: POSPort
    messaging: MessagingPort
    reviews: ReviewsPort
    supplier: SupplierPort
    social: SocialPort
    payroll: PayrollPort
    bank: BankPort
    meta: dict[str, Any] = field(default_factory=dict)
