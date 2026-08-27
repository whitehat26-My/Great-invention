"""Simulated supplier.

Accepts purchase orders, then delivers and invoices against them — imperfectly,
on purpose. Roughly one delivery in five is short, and one invoice in six carries
a price above the contracted rate. Those are the cases the three-way match
exists to catch, so a simulator that always behaved would leave the invoice agent
untested against the only thing it is for.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal

from restaurant_ai import clock
from restaurant_ai.integrations.base import DeliveryNote, SupplierInvoiceDoc

SHORT_DELIVERY_RATE = 0.20
PRICE_DRIFT_RATE = 0.17
# Suppliers creep prices up rather than doubling them; small drifts are what
# actually erode margin unnoticed.
PRICE_DRIFT_RANGE = (Decimal("1.03"), Decimal("1.14"))


@dataclass
class _SentOrder:
    po_number: str
    supplier_code: str
    lines: list[tuple[str, Decimal]]
    sent_at: datetime
    unit_prices: dict[str, Decimal] = field(default_factory=dict)
    delivered: bool = False
    invoiced: bool = False


class FakeSupplier:
    provider = "fake_supplier"

    def __init__(self, seed: int | None = None, lead_days: int = 1) -> None:
        self._seed = seed
        self.lead_days = lead_days
        self._orders: dict[str, _SentOrder] = {}
        self._counter = 0

    def _rng(self, key: str) -> random.Random:
        # Seeded from the PO number, so the same order always behaves the same.
        base = self._seed if self._seed is not None else 0
        return random.Random(hash((key, base)) & 0xFFFFFFFF)

    def send_purchase_order(
        self,
        po_number: str,
        supplier_code: str,
        lines: list[tuple[str, Decimal]],
        unit_prices: dict[str, Decimal] | None = None,
    ) -> str:
        self._orders[po_number] = _SentOrder(
            po_number=po_number,
            supplier_code=supplier_code,
            lines=list(lines),
            sent_at=clock.now(),
            unit_prices=dict(unit_prices or {}),
        )
        self._counter += 1
        return f"ACK-{po_number}"

    def fetch_deliveries(self, since: datetime) -> list[DeliveryNote]:
        """Deliver any order whose lead time has elapsed, sometimes short."""
        notes: list[DeliveryNote] = []
        now = clock.now()
        for order in self._orders.values():
            if order.delivered:
                continue
            due = order.sent_at + timedelta(days=self.lead_days)
            if now < due:
                continue

            rng = self._rng(order.po_number + ":delivery")
            lines: list[tuple[str, Decimal]] = []
            shorted: list[str] = []
            for sku, packs in order.lines:
                if rng.random() < SHORT_DELIVERY_RATE and packs > 1:
                    delivered = (packs - Decimal("1")).quantize(Decimal("0.0001"))
                    shorted.append(sku)
                else:
                    delivered = packs
                lines.append((sku, delivered))

            order.delivered = True
            notes.append(
                DeliveryNote(
                    po_number=order.po_number,
                    delivered_at=due.replace(hour=7, minute=30),
                    lines=lines,
                    note=(
                        f"Short on {', '.join(shorted)} - backorder to follow" if shorted else None
                    ),
                )
            )
        return notes

    def fetch_invoices(self, since: datetime) -> list[SupplierInvoiceDoc]:
        """Invoice each delivered order, occasionally above the agreed price."""
        invoices: list[SupplierInvoiceDoc] = []
        for order in self._orders.values():
            if order.invoiced or not order.delivered:
                continue

            rng = self._rng(order.po_number + ":invoice")
            lines: list[tuple[str, Decimal, Decimal]] = []
            for sku, packs in order.lines:
                price = order.unit_prices.get(sku, Decimal("10.00"))
                if rng.random() < PRICE_DRIFT_RATE:
                    low, high = PRICE_DRIFT_RANGE
                    drift = low + (high - low) * Decimal(str(rng.random()))
                    price = (price * drift).quantize(Decimal("0.01"))
                lines.append((sku, packs, price))

            order.invoiced = True
            invoice_date = (order.sent_at + timedelta(days=self.lead_days)).date()
            invoices.append(
                SupplierInvoiceDoc(
                    invoice_number=f"INV-{order.po_number.split('-')[-1]}",
                    supplier_code=order.supplier_code,
                    po_number=order.po_number,
                    invoice_date=invoice_date,
                    lines=lines,
                    tax=Decimal("0"),
                    document_uri=f"file://invoices/{order.po_number}.pdf",
                )
            )
        return invoices

    @property
    def sent_orders(self) -> dict[str, _SentOrder]:
        return dict(self._orders)
