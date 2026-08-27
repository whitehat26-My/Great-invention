"""Suppliers, stock, purchasing and the three-way match.

``stock_movement`` is append-only: on-hand is the sum of movements, never a
mutated column. That makes every deduction auditable back to the POS sale or
delivery that caused it, which is what the reconciliation agent relies on.
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
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from restaurant_ai.db.base import Base, Money, Qty, Timestamped, UUIDPk
from restaurant_ai.db.models.enums import (
    DiscrepancyKind,
    InvoiceStatus,
    MovementReason,
    PurchaseOrderStatus,
)


class Supplier(UUIDPk, Timestamped, Base):
    __tablename__ = "supplier"

    code: Mapped[str] = mapped_column(String(40), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(160))
    email: Mapped[str | None] = mapped_column(String(160))
    phone: Mapped[str | None] = mapped_column(String(40))
    lead_time_days: Mapped[int] = mapped_column(Integer, default=2)
    min_order_value: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"))
    # Days of the week this supplier delivers, e.g. "0,2,4" (Mon/Wed/Fri).
    delivery_days: Mapped[str] = mapped_column(String(20), default="0,1,2,3,4,5,6")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    stock_items: Mapped[list[StockItem]] = relationship(back_populates="supplier")


class StockItem(UUIDPk, Timestamped, Base):
    """A supplier's purchasable pack for an ingredient (e.g. 5 kg box of onions)."""

    __tablename__ = "stock_item"

    ingredient_id: Mapped[str] = mapped_column(ForeignKey("ingredient.id"), index=True)
    supplier_id: Mapped[str] = mapped_column(ForeignKey("supplier.id"), index=True)
    supplier_sku: Mapped[str] = mapped_column(String(60))
    pack_size: Mapped[Decimal] = mapped_column(Qty, doc="Base units per purchased pack.")
    pack_uom: Mapped[str] = mapped_column(String(12), default="ea")
    contract_price: Mapped[Decimal] = mapped_column(Money, doc="Agreed price per pack.")
    min_order_qty: Mapped[Decimal] = mapped_column(Qty, default=Decimal("1"))
    is_preferred: Mapped[bool] = mapped_column(Boolean, default=True)

    ingredient: Mapped[object] = relationship("Ingredient")
    supplier: Mapped[Supplier] = relationship(back_populates="stock_items")

    __table_args__ = (
        UniqueConstraint("supplier_id", "supplier_sku", name="uq_stock_item_supplier_sku"),
        CheckConstraint("pack_size > 0", name="pack_size_positive"),
    )


class ReorderPolicy(UUIDPk, Timestamped, Base):
    """Per-ingredient reorder configuration, refreshed from observed demand."""

    __tablename__ = "reorder_policy"

    ingredient_id: Mapped[str] = mapped_column(ForeignKey("ingredient.id"), unique=True)
    # Computed by domain.inventory: lead-time demand + safety stock.
    reorder_point: Mapped[Decimal] = mapped_column(Qty, default=Decimal("0"))
    safety_stock: Mapped[Decimal] = mapped_column(Qty, default=Decimal("0"))
    target_days_cover: Mapped[int] = mapped_column(Integer, default=7)
    avg_daily_usage: Mapped[Decimal] = mapped_column(Qty, default=Decimal("0"))
    usage_stddev: Mapped[Decimal] = mapped_column(Qty, default=Decimal("0"))
    last_recalculated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class StockMovement(UUIDPk, Base):
    """Append-only stock ledger. Positive = in, negative = out."""

    __tablename__ = "stock_movement"

    ingredient_id: Mapped[str] = mapped_column(ForeignKey("ingredient.id"), index=True)
    quantity: Mapped[Decimal] = mapped_column(Qty, doc="In ingredient base units; signed.")
    reason: Mapped[MovementReason] = mapped_column(
        Enum(MovementReason, native_enum=False, length=24)
    )
    unit_cost: Mapped[Decimal] = mapped_column(Qty, default=Decimal("0"))
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    # Free-form provenance: the order line, goods receipt or count that caused it.
    source_type: Mapped[str | None] = mapped_column(String(40))
    source_id: Mapped[str | None] = mapped_column(String(64))
    note: Mapped[str | None] = mapped_column(String(300))

    __table_args__ = (
        Index("ix_stock_movement_ingredient_time", "ingredient_id", "occurred_at"),
        CheckConstraint("quantity <> 0", name="quantity_non_zero"),
    )


class PurchaseOrder(UUIDPk, Timestamped, Base):
    __tablename__ = "purchase_order"

    po_number: Mapped[str] = mapped_column(String(40), unique=True, index=True)
    supplier_id: Mapped[str] = mapped_column(ForeignKey("supplier.id"), index=True)
    status: Mapped[PurchaseOrderStatus] = mapped_column(
        Enum(PurchaseOrderStatus, native_enum=False, length=24),
        default=PurchaseOrderStatus.DRAFT,
        index=True,
    )
    expected_delivery_on: Mapped[date | None] = mapped_column(Date)
    subtotal: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"))
    tax: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"))
    total: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"))
    rationale: Mapped[str | None] = mapped_column(Text, doc="Why the agent drafted this PO.")
    created_by_run_id: Mapped[str | None] = mapped_column(String(36), index=True)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    approved_by: Mapped[str | None] = mapped_column(String(120))

    supplier: Mapped[Supplier] = relationship()
    lines: Mapped[list[PurchaseOrderLine]] = relationship(
        back_populates="purchase_order", cascade="all, delete-orphan"
    )


class PurchaseOrderLine(UUIDPk, Base):
    __tablename__ = "purchase_order_line"

    purchase_order_id: Mapped[str] = mapped_column(ForeignKey("purchase_order.id"), index=True)
    stock_item_id: Mapped[str] = mapped_column(ForeignKey("stock_item.id"))
    quantity_packs: Mapped[Decimal] = mapped_column(Qty)
    unit_price: Mapped[Decimal] = mapped_column(Money)
    line_total: Mapped[Decimal] = mapped_column(Money)
    received_packs: Mapped[Decimal] = mapped_column(Qty, default=Decimal("0"))

    purchase_order: Mapped[PurchaseOrder] = relationship(back_populates="lines")
    stock_item: Mapped[StockItem] = relationship()

    __table_args__ = (CheckConstraint("quantity_packs > 0", name="quantity_packs_positive"),)


class GoodsReceipt(UUIDPk, Timestamped, Base):
    """What physically arrived — the middle leg of the three-way match."""

    __tablename__ = "goods_receipt"

    purchase_order_id: Mapped[str] = mapped_column(ForeignKey("purchase_order.id"), index=True)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    received_by: Mapped[str | None] = mapped_column(String(120))
    note: Mapped[str | None] = mapped_column(Text)

    purchase_order: Mapped[PurchaseOrder] = relationship()
    lines: Mapped[list[GoodsReceiptLine]] = relationship(
        back_populates="goods_receipt", cascade="all, delete-orphan"
    )


class GoodsReceiptLine(UUIDPk, Base):
    __tablename__ = "goods_receipt_line"

    goods_receipt_id: Mapped[str] = mapped_column(ForeignKey("goods_receipt.id"), index=True)
    purchase_order_line_id: Mapped[str] = mapped_column(ForeignKey("purchase_order_line.id"))
    quantity_packs: Mapped[Decimal] = mapped_column(Qty)

    goods_receipt: Mapped[GoodsReceipt] = relationship(back_populates="lines")
    purchase_order_line: Mapped[PurchaseOrderLine] = relationship()


class SupplierInvoice(UUIDPk, Timestamped, Base):
    """What the supplier billed — the third leg of the match."""

    __tablename__ = "supplier_invoice"

    invoice_number: Mapped[str] = mapped_column(String(60), index=True)
    supplier_id: Mapped[str] = mapped_column(ForeignKey("supplier.id"), index=True)
    purchase_order_id: Mapped[str | None] = mapped_column(ForeignKey("purchase_order.id"))
    invoice_date: Mapped[date] = mapped_column(Date)
    subtotal: Mapped[Decimal] = mapped_column(Money)
    tax: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"))
    total: Mapped[Decimal] = mapped_column(Money)
    status: Mapped[InvoiceStatus] = mapped_column(
        Enum(InvoiceStatus, native_enum=False, length=24),
        default=InvoiceStatus.RECEIVED,
        index=True,
    )
    document_uri: Mapped[str | None] = mapped_column(String(400), doc="Digitised receipt image.")
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    approved_by: Mapped[str | None] = mapped_column(String(120))

    supplier: Mapped[Supplier] = relationship()
    purchase_order: Mapped[PurchaseOrder | None] = relationship()
    lines: Mapped[list[SupplierInvoiceLine]] = relationship(
        back_populates="invoice", cascade="all, delete-orphan"
    )
    discrepancies: Mapped[list[InvoiceDiscrepancy]] = relationship(
        back_populates="invoice", cascade="all, delete-orphan"
    )

    __table_args__ = (
        UniqueConstraint("supplier_id", "invoice_number", name="uq_supplier_invoice_number"),
    )


class SupplierInvoiceLine(UUIDPk, Base):
    __tablename__ = "supplier_invoice_line"

    invoice_id: Mapped[str] = mapped_column(ForeignKey("supplier_invoice.id"), index=True)
    stock_item_id: Mapped[str | None] = mapped_column(ForeignKey("stock_item.id"))
    description: Mapped[str] = mapped_column(String(240))
    quantity_packs: Mapped[Decimal] = mapped_column(Qty)
    unit_price: Mapped[Decimal] = mapped_column(Money)
    line_total: Mapped[Decimal] = mapped_column(Money)

    invoice: Mapped[SupplierInvoice] = relationship(back_populates="lines")
    stock_item: Mapped[StockItem | None] = relationship()


class InvoiceDiscrepancy(UUIDPk, Timestamped, Base):
    """A flagged mismatch between PO, receipt and invoice."""

    __tablename__ = "invoice_discrepancy"

    invoice_id: Mapped[str] = mapped_column(ForeignKey("supplier_invoice.id"), index=True)
    invoice_line_id: Mapped[str | None] = mapped_column(ForeignKey("supplier_invoice_line.id"))
    kind: Mapped[DiscrepancyKind] = mapped_column(
        Enum(DiscrepancyKind, native_enum=False, length=24)
    )
    expected: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"))
    actual: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"))
    variance: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"))
    detail: Mapped[str] = mapped_column(Text)
    resolved: Mapped[bool] = mapped_column(Boolean, default=False)

    invoice: Mapped[SupplierInvoice] = relationship(back_populates="discrepancies")
