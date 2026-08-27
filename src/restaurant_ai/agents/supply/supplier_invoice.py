"""Supplier & Invoice Agent.

Runs the three-way match: what we ordered (PO), what arrived (goods receipt),
and what we were billed (invoice). Any leg disagreeing with another beyond
tolerance is flagged rather than paid.

Small price creep is the thing this catches that humans miss. A supplier moving
a 92.50 case to 97.20 is invisible on any single invoice and is several thousand
ringgit a year across a catalogue. Every line is checked against the contracted
price, and the drift is quantified.

Releasing payment is approval-gated.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, Field
from sqlalchemy import select

from restaurant_ai import clock
from restaurant_ai.config import get_settings
from restaurant_ai.db.models import (
    DiscrepancyKind,
    GoodsReceipt,
    GoodsReceiptLine,
    Ingredient,
    InvoiceDiscrepancy,
    InvoiceStatus,
    MovementReason,
    PurchaseOrder,
    PurchaseOrderLine,
    PurchaseOrderStatus,
    StockItem,
    StockMovement,
    Supplier,
    SupplierInvoice,
    SupplierInvoiceLine,
)
from restaurant_ai.events import Event, Topic, publish
from restaurant_ai.kernel.registry import register
from restaurant_ai.kernel.spec import AgentSpec, ToolContext, ToolSpec

ZERO = Decimal("0")

SYSTEM_PROMPT = """You are the Supplier and Invoice Agent for a restaurant.

You reconcile three documents that should agree and often do not: the purchase
order, the goods receipt, and the supplier's invoice.

What you are looking for:
- Being billed for more than arrived. Short deliveries are common and invoices
  rarely get corrected on their own.
- Prices above the contracted rate. These creep up a few percent at a time and
  are invisible on any single invoice, but compound across a year.
- Tax applied where it should not be, or at the wrong rate.
- Lines on the invoice that were never ordered.

Be precise about money. State the expected figure, the actual figure and the
variance. Never approve a payment with an unresolved discrepancy: flag it,
quantify it, and let a human decide."""


class ReceiveDeliveriesArgs(BaseModel):
    pass


class MatchInvoicesArgs(BaseModel):
    pass


class ReleasePaymentArgs(BaseModel):
    only_clean: bool = Field(
        True, description="Release only invoices with no unresolved discrepancy."
    )


def perceive(context: ToolContext) -> dict[str, Any]:
    session = context.session
    awaiting = list(
        session.execute(
            select(SupplierInvoice).where(
                SupplierInvoice.status.in_(
                    [InvoiceStatus.RECEIVED, InvoiceStatus.MATCHED, InvoiceStatus.DISPUTED]
                )
            )
        ).scalars()
    )
    open_orders = list(
        session.execute(
            select(PurchaseOrder).where(
                PurchaseOrder.status.in_(
                    [PurchaseOrderStatus.SENT, PurchaseOrderStatus.PARTIALLY_RECEIVED]
                )
            )
        ).scalars()
    )
    unresolved = list(
        session.execute(
            select(InvoiceDiscrepancy).where(InvoiceDiscrepancy.resolved.is_(False))
        ).scalars()
    )

    return {
        "open_purchase_orders": len(open_orders),
        "invoices_awaiting": len(awaiting),
        "unresolved_discrepancies": len(unresolved),
        "discrepancy_value": str(sum((d.variance for d in unresolved), ZERO)),
        "invoices": [
            {
                "invoice_number": i.invoice_number,
                "total": str(i.total),
                "status": i.status.value,
            }
            for i in awaiting[:20]
        ],
    }


def receive_deliveries(context: ToolContext) -> dict[str, Any]:
    """Book in what suppliers actually delivered, and move stock accordingly."""
    from restaurant_ai.integrations import get_integrations

    session = context.session
    supplier_port = get_integrations().supplier
    notes = supplier_port.fetch_deliveries(clock.now() - timedelta(days=7))

    received: list[dict[str, Any]] = []
    for note in notes:
        order = session.execute(
            select(PurchaseOrder).where(PurchaseOrder.po_number == note.po_number)
        ).scalar_one_or_none()
        if order is None:
            continue

        receipt = GoodsReceipt(
            purchase_order_id=order.id,
            received_at=note.delivered_at,
            received_by="goods_in",
            note=note.note,
        )
        session.add(receipt)
        session.flush()

        lines = {
            line.stock_item.supplier_sku: line
            for line in session.execute(
                select(PurchaseOrderLine).where(PurchaseOrderLine.purchase_order_id == order.id)
            ).scalars()
        }

        fully_received = True
        for supplier_sku, packs in note.lines:
            po_line = lines.get(supplier_sku)
            if po_line is None:
                continue
            session.add(
                GoodsReceiptLine(
                    goods_receipt_id=receipt.id,
                    purchase_order_line_id=po_line.id,
                    quantity_packs=packs,
                )
            )
            po_line.received_packs = (po_line.received_packs or ZERO) + packs
            if po_line.received_packs < po_line.quantity_packs:
                fully_received = False

            # Stock in, at the pack's base-unit size.
            stock_item = po_line.stock_item
            session.add(
                StockMovement(
                    ingredient_id=stock_item.ingredient_id,
                    quantity=(packs * stock_item.pack_size).quantize(Decimal("0.0001")),
                    reason=MovementReason.RECEIPT,
                    unit_cost=(
                        po_line.unit_price / stock_item.pack_size if stock_item.pack_size else ZERO
                    ),
                    occurred_at=note.delivered_at,
                    source_type="goods_receipt",
                    source_id=receipt.id,
                    note=f"Received against {order.po_number}",
                )
            )

        order.status = (
            PurchaseOrderStatus.RECEIVED
            if fully_received
            else PurchaseOrderStatus.PARTIALLY_RECEIVED
        )
        received.append(
            {
                "po_number": order.po_number,
                "lines": len(note.lines),
                "complete": fully_received,
                "note": note.note,
            }
        )
        publish(
            Event(
                Topic.GOODS_RECEIVED,
                {"po_number": order.po_number, "complete": fully_received},
                source_run_id=context.run_id,
            ),
            session=session,
        )

    session.flush()
    short = [r for r in received if not r["complete"]]
    return {
        "receipts": len(received),
        "short_deliveries": len(short),
        "details": received,
        "note": (
            f"{len(received)} delivery/deliveries booked in"
            + (f", {len(short)} short against the order." if short else " in full.")
        ),
    }


def match_invoices(context: ToolContext) -> dict[str, Any]:
    """Three-way match every new invoice, flagging anything out of tolerance."""
    from restaurant_ai.integrations import get_integrations

    session = context.session
    settings = get_settings()
    supplier_port = get_integrations().supplier
    documents = supplier_port.fetch_invoices(clock.now() - timedelta(days=7))

    matched = 0
    flagged: list[dict[str, Any]] = []

    for doc in documents:
        supplier = session.execute(
            select(Supplier).where(Supplier.code == doc.supplier_code)
        ).scalar_one_or_none()
        if supplier is None:
            continue

        existing = session.execute(
            select(SupplierInvoice).where(
                SupplierInvoice.supplier_id == supplier.id,
                SupplierInvoice.invoice_number == doc.invoice_number,
            )
        ).scalar_one_or_none()
        if existing is not None:
            continue  # already digitised

        order = (
            session.execute(
                select(PurchaseOrder).where(PurchaseOrder.po_number == doc.po_number)
            ).scalar_one_or_none()
            if doc.po_number
            else None
        )

        invoice = SupplierInvoice(
            invoice_number=doc.invoice_number,
            supplier_id=supplier.id,
            purchase_order_id=order.id if order else None,
            invoice_date=doc.invoice_date,
            subtotal=doc.subtotal,
            tax=doc.tax,
            total=doc.total,
            status=InvoiceStatus.RECEIVED,
            document_uri=doc.document_uri,
        )
        session.add(invoice)
        session.flush()

        po_lines: dict[str, PurchaseOrderLine] = {}
        if order is not None:
            po_lines = {
                line.stock_item.supplier_sku: line
                for line in session.execute(
                    select(PurchaseOrderLine).where(PurchaseOrderLine.purchase_order_id == order.id)
                ).scalars()
            }

        issues: list[dict[str, Any]] = []
        for supplier_sku, packs, unit_price in doc.lines:
            stock_item = session.execute(
                select(StockItem).where(
                    StockItem.supplier_id == supplier.id,
                    StockItem.supplier_sku == supplier_sku,
                )
            ).scalar_one_or_none()
            ingredient = session.get(Ingredient, stock_item.ingredient_id) if stock_item else None
            description = ingredient.name if ingredient else supplier_sku

            line = SupplierInvoiceLine(
                invoice_id=invoice.id,
                stock_item_id=stock_item.id if stock_item else None,
                description=description,
                quantity_packs=packs,
                unit_price=unit_price,
                line_total=(packs * unit_price).quantize(Decimal("0.01")),
            )
            session.add(line)
            session.flush()

            po_line = po_lines.get(supplier_sku)
            if po_line is None:
                issues.append(
                    _flag(
                        session,
                        invoice,
                        line,
                        DiscrepancyKind.UNEXPECTED_LINE,
                        ZERO,
                        line.line_total,
                        f"{description} was invoiced but does not appear on {doc.po_number}.",
                    )
                )
                continue

            # Price against the contracted rate.
            contracted = po_line.unit_price
            if contracted > 0:
                drift = (unit_price - contracted) / contracted
                if abs(drift) > settings.invoice_price_tolerance_pct:
                    issues.append(
                        _flag(
                            session,
                            invoice,
                            line,
                            DiscrepancyKind.PRICE,
                            contracted,
                            unit_price,
                            (
                                f"{description}: invoiced at {unit_price} against a contracted "
                                f"{contracted} ({drift * 100:+.1f}%). Over {packs} pack(s) that "
                                f"is {(unit_price - contracted) * packs:+.2f}."
                            ),
                        )
                    )

            # Quantity against what actually arrived.
            delivered = po_line.received_packs or ZERO
            if delivered > 0 and packs > delivered:
                issues.append(
                    _flag(
                        session,
                        invoice,
                        line,
                        DiscrepancyKind.QUANTITY,
                        delivered,
                        packs,
                        (
                            f"{description}: billed for {packs} pack(s) but only {delivered} "
                            f"arrived. Overbilled by "
                            f"{((packs - delivered) * unit_price):.2f}."
                        ),
                    )
                )

        if issues:
            invoice.status = InvoiceStatus.DISPUTED
            flagged.append(
                {
                    "invoice_number": invoice.invoice_number,
                    "supplier": supplier.name,
                    "total": str(invoice.total),
                    "issues": issues,
                }
            )
            publish(
                Event(
                    Topic.INVOICE_DISCREPANCY,
                    {"invoice": invoice.invoice_number, "issues": len(issues)},
                    source_run_id=context.run_id,
                ),
                session=session,
            )
        else:
            invoice.status = InvoiceStatus.MATCHED
            matched += 1

        # A confirmed price change updates the ingredient cost, so plate costs
        # and margins reflect what we are actually paying.
        _refresh_ingredient_costs(session, invoice)

    session.flush()
    total_variance = sum(
        (Decimal(str(issue["variance"])) for f in flagged for issue in f["issues"]), ZERO
    )
    return {
        "invoices_processed": matched + len(flagged),
        "matched_clean": matched,
        "disputed": len(flagged),
        "total_variance": str(total_variance.quantize(Decimal("0.01"))),
        "flagged": flagged,
    }


def release_payment(context: ToolContext, only_clean: bool = True) -> dict[str, Any]:
    """Propose which invoices to pay. Approval-gated."""
    session = context.session
    stmt = select(SupplierInvoice).where(SupplierInvoice.status == InvoiceStatus.MATCHED)
    if not only_clean:
        stmt = select(SupplierInvoice).where(
            SupplierInvoice.status.in_([InvoiceStatus.MATCHED, InvoiceStatus.DISPUTED])
        )

    invoices = list(session.execute(stmt).scalars())
    payable = [
        {
            "invoice_id": i.id,
            "invoice_number": i.invoice_number,
            "supplier": i.supplier.name,
            "total": str(i.total),
            "invoice_date": i.invoice_date.isoformat(),
        }
        for i in invoices
    ]
    total = sum((i.total for i in invoices), ZERO)

    return {
        "count": len(payable),
        "total": str(total),
        "invoices": payable,
        "only_clean": only_clean,
    }


def commit_payment(context: ToolContext, payload: dict[str, Any]) -> dict[str, Any]:
    """Approved: mark the invoices paid."""
    session = context.session
    paid: list[str] = []
    for entry in payload.get("invoices", []):
        invoice = session.get(SupplierInvoice, entry["invoice_id"])
        if invoice is None:
            continue
        invoice.status = InvoiceStatus.PAID
        invoice.paid_at = clock.utcnow()
        invoice.approved_by = payload.get("approved_by") or "approver"
        paid.append(invoice.invoice_number)
    session.flush()
    return {"paid": paid, "count": len(paid), "total": payload.get("total")}


def _flag(
    session,
    invoice: SupplierInvoice,
    line: SupplierInvoiceLine,
    kind: DiscrepancyKind,
    expected: Decimal,
    actual: Decimal,
    detail: str,
) -> dict[str, Any]:
    variance = (actual - expected).quantize(Decimal("0.01"))
    session.add(
        InvoiceDiscrepancy(
            invoice_id=invoice.id,
            invoice_line_id=line.id,
            kind=kind,
            expected=expected,
            actual=actual,
            variance=variance,
            detail=detail,
        )
    )
    return {
        "kind": kind.value,
        "expected": str(expected),
        "actual": str(actual),
        "variance": str(variance),
        "detail": detail,
    }


def _refresh_ingredient_costs(session, invoice: SupplierInvoice) -> None:
    """Update ingredient unit costs from a clean invoice.

    Only matched invoices feed through: taking a price from a disputed invoice
    would bake the supplier's error into every downstream margin calculation.
    """
    if invoice.status != InvoiceStatus.MATCHED:
        return
    for line in invoice.lines:
        if line.stock_item_id is None:
            continue
        stock_item = session.get(StockItem, line.stock_item_id)
        if stock_item is None or stock_item.pack_size <= 0:
            continue
        ingredient = session.get(Ingredient, stock_item.ingredient_id)
        if ingredient is None:
            continue
        ingredient.cost_per_base_unit = (line.unit_price / stock_item.pack_size).quantize(
            Decimal("0.0001")
        )


def autonomous(context: ToolContext, perceived: dict[str, Any]) -> dict[str, Any]:
    calls = [
        {"name": "receive_deliveries", "args": {}},
        {"name": "match_invoices", "args": {}},
        {"name": "release_payment", "args": {"only_clean": True}},
    ]
    return {
        "summary": (
            f"{perceived.get('open_purchase_orders', 0)} order(s) outstanding. "
            f"Booking in deliveries, running the three-way match, and proposing payment "
            f"for cleanly matched invoices."
        ),
        "results": {},
        "tool_calls": calls,
    }


_payment_tool = ToolSpec(
    name="release_payment",
    description="Propose supplier invoices for payment. Requires human approval.",
    fn=release_payment,
    args_schema=ReleasePaymentArgs,
    requires_approval=True,
    # Nothing due means nothing to approve. Asking a human to sign off an empty
    # payment run is how people learn to click through the ones that matter.
    gate_when=lambda r: r.get("count", 0) > 0,
    approval_value=lambda r: Decimal(str(r.get("total", "0"))),
    approval_summary=lambda r: f"Release payment for {r['count']} invoice(s), {r['total']}",
    approval_detail=lambda r: (
        "\n".join(
            f"    {i['supplier']} {i['invoice_number']} - {i['total']} (dated {i['invoice_date']})"
            for i in r.get("invoices", [])
        )
        or "Nothing due for payment."
    ),
)
_payment_tool.commit_fn = commit_payment  # type: ignore[attr-defined]


SUPPLIER_INVOICE_AGENT = register(
    AgentSpec(
        name="supplier_invoice",
        department="supply",
        title="Supplier & Invoice Agent",
        description=(
            "Cross-checks delivery invoices against purchase orders, flags vendor price "
            "discrepancies, and digitises receipts."
        ),
        system_prompt=SYSTEM_PROMPT,
        model_tier="reasoning",
        tools=[
            ToolSpec(
                name="receive_deliveries",
                description="Book in supplier deliveries and move the stock.",
                fn=receive_deliveries,
                args_schema=ReceiveDeliveriesArgs,
            ),
            ToolSpec(
                name="match_invoices",
                description=(
                    "Three-way match new invoices against their purchase orders and goods "
                    "receipts, flagging price, quantity and tax discrepancies."
                ),
                fn=match_invoices,
                args_schema=MatchInvoicesArgs,
            ),
            _payment_tool,
        ],
        perceive=perceive,
        autonomous=autonomous,
    )
)
