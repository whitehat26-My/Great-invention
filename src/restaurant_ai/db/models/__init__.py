"""All ORM models.

Importing this package registers every table on ``Base.metadata``, which is what
Alembic autogenerate and the test fixtures rely on.
"""

from restaurant_ai.db.models.agent import (
    AgentAction,
    AgentRun,
    ApprovalRequest,
    ConversationTurn,
    InboundEvent,
    OutboxEvent,
    Reminder,
)
from restaurant_ai.db.models.enums import (
    AccountType,
    AgentRunStatus,
    ApprovalStatus,
    DiscrepancyKind,
    InvoiceStatus,
    MenuClass,
    MovementReason,
    OrderChannel,
    OrderStatus,
    PaymentMethod,
    PurchaseOrderStatus,
    ReservationStatus,
    ReviewSentiment,
    ShiftRole,
    Station,
    TicketStatus,
)
from restaurant_ai.db.models.foh import Reservation, SeatingEvent, TableDef
from restaurant_ai.db.models.inventory import (
    GoodsReceipt,
    GoodsReceiptLine,
    InvoiceDiscrepancy,
    PurchaseOrder,
    PurchaseOrderLine,
    ReorderPolicy,
    StockItem,
    StockMovement,
    Supplier,
    SupplierInvoice,
    SupplierInvoiceLine,
)
from restaurant_ai.db.models.kitchen import ItemForecast, KdsTicket, PrepPlan, PrepPlanLine
from restaurant_ai.db.models.ledger import (
    DailyReport,
    JournalEntry,
    JournalLine,
    LedgerAccount,
    ReconciliationBatch,
    ReconciliationMatch,
)
from restaurant_ai.db.models.marketing import Campaign, PromoOffer, Review, SocialPost
from restaurant_ai.db.models.menu import (
    Allergen,
    Ingredient,
    MenuItem,
    MenuItemPriceHistory,
    MenuSection,
    Recipe,
    RecipeComponent,
    UomConversion,
)
from restaurant_ai.db.models.sales import (
    BankTransaction,
    DeliveryPayout,
    Guest,
    OrderHeader,
    OrderLine,
    Payment,
)
from restaurant_ai.db.models.workforce import (
    Availability,
    Shift,
    ShiftAssignment,
    SopDocument,
    Staff,
    TimeEntry,
)

__all__ = [
    # enums
    "AccountType",
    "AgentRunStatus",
    "ApprovalStatus",
    "DiscrepancyKind",
    "InvoiceStatus",
    "MenuClass",
    "MovementReason",
    "OrderChannel",
    "OrderStatus",
    "PaymentMethod",
    "PurchaseOrderStatus",
    "ReservationStatus",
    "ReviewSentiment",
    "ShiftRole",
    "Station",
    "TicketStatus",
    # menu / bom
    "Allergen",
    "Ingredient",
    "MenuItem",
    "MenuItemPriceHistory",
    "MenuSection",
    "Recipe",
    "RecipeComponent",
    "UomConversion",
    # inventory
    "GoodsReceipt",
    "GoodsReceiptLine",
    "InvoiceDiscrepancy",
    "PurchaseOrder",
    "PurchaseOrderLine",
    "ReorderPolicy",
    "StockItem",
    "StockMovement",
    "Supplier",
    "SupplierInvoice",
    "SupplierInvoiceLine",
    # sales
    "BankTransaction",
    "DeliveryPayout",
    "Guest",
    "OrderHeader",
    "OrderLine",
    "Payment",
    # ledger
    "DailyReport",
    "JournalEntry",
    "JournalLine",
    "LedgerAccount",
    "ReconciliationBatch",
    "ReconciliationMatch",
    # foh / kitchen
    "Reservation",
    "SeatingEvent",
    "TableDef",
    "ItemForecast",
    "KdsTicket",
    "PrepPlan",
    "PrepPlanLine",
    # workforce / marketing
    "Availability",
    "Shift",
    "ShiftAssignment",
    "SopDocument",
    "Staff",
    "TimeEntry",
    "Campaign",
    "PromoOffer",
    "Review",
    "SocialPost",
    # agent plane
    "AgentAction",
    "AgentRun",
    "ConversationTurn",
    "Reminder",
    "ApprovalRequest",
    "InboundEvent",
    "OutboxEvent",
]
