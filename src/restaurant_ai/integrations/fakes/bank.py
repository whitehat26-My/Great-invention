"""Simulated merchant statements and delivery-platform payouts.

Card settlements land a day late and net of processing fees; platform payouts
arrive weekly net of commission. Both behaviours are exactly why the day's
takings never match the bank on the day, and are what the reconciliation agent
has to account for.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import select

from restaurant_ai.db.base import session_scope
from restaurant_ai.db.models import DeliveryPayout, Payment, PaymentMethod
from restaurant_ai.integrations.base import PlatformPayout, SettlementLine

CARD_FEE_RATE = Decimal("0.018")
PLATFORM_COMMISSION = {"GrabFood": Decimal("0.30"), "foodpanda": Decimal("0.28")}


class FakeBank:
    provider = "fake_bank"

    def __init__(self, seed: int | None = None, settle_lag_days: int = 1) -> None:
        self._seed = seed
        self.settle_lag_days = settle_lag_days

    def fetch_settlements(self, business_date: date) -> list[SettlementLine]:
        """Mirror the day's card/e-wallet takings as statement lines.

        Built from what the POS actually recorded, so a genuine mismatch shows
        up as a real exception rather than simulator noise.
        """
        with session_scope() as session:
            payments = list(
                session.execute(
                    select(Payment).where(
                        Payment.business_date == business_date,
                        Payment.method.in_([PaymentMethod.CARD, PaymentMethod.EWALLET]),
                    )
                ).scalars()
            )

        posted = business_date + timedelta(days=self.settle_lag_days)
        return [
            SettlementLine(
                reference=payment.processor_ref or f"TXN-{payment.id[:8]}",
                amount=payment.amount + payment.tip,
                posted_on=posted,
                description=f"Card settlement {payment.method.value}",
                counterparty="Merchant Acquirer",
            )
            for payment in payments
        ]

    def fetch_payouts(self, since: date) -> list[PlatformPayout]:
        """Weekly platform settlements, gross less commission."""
        with session_scope() as session:
            rows = list(
                session.execute(
                    select(DeliveryPayout).where(DeliveryPayout.period_start >= since)
                ).scalars()
            )
        return [
            PlatformPayout(
                platform=row.platform,
                payout_ref=row.payout_ref,
                period_start=row.period_start,
                period_end=row.period_end,
                gross_sales=row.gross_sales,
                commission=row.commission,
                adjustments=row.adjustments,
            )
            for row in rows
        ]

    @staticmethod
    def commission_for(platform: str, gross: Decimal) -> Decimal:
        rate = PLATFORM_COMMISSION.get(platform, Decimal("0.28"))
        return (gross * rate).quantize(Decimal("0.01"))

    @staticmethod
    def card_fee(amount: Decimal) -> Decimal:
        return (amount * CARD_FEE_RATE).quantize(Decimal("0.01"))
