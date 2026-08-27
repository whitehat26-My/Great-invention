"""Integration ports and their simulators.

Two properties matter. Determinism, because the simulated service day doubles as
a regression test and a day that replays differently each time is useless for
that. And imperfection: the supplier has to short-deliver and over-charge
sometimes, or the three-way match is never exercised against the only cases it
exists for.
"""

from __future__ import annotations

from collections import Counter
from datetime import date, timedelta
from decimal import Decimal

import pytest

from restaurant_ai import clock
from restaurant_ai.integrations.base import (
    BankPort,
    MessagingPort,
    PosOrder,
    PosOrderLine,
    POSPort,
    ReviewsPort,
    ScheduledPost,
    SocialPort,
    SupplierPort,
)
from restaurant_ai.integrations.fakes import (
    FakeBank,
    FakeMessaging,
    FakePOS,
    FakeReviews,
    FakeSocial,
    FakeSupplier,
)

D = Decimal
DAY = date(2026, 8, 28)


class TestProtocolConformance:
    """Every fake must satisfy the port agents depend on."""

    @pytest.mark.parametrize(
        ("fake", "port"),
        [
            (FakePOS(), POSPort),
            (FakeMessaging(), MessagingPort),
            (FakeReviews(), ReviewsPort),
            (FakeSupplier(), SupplierPort),
            (FakeSocial(), SocialPort),
            (FakeBank(), BankPort),
        ],
    )
    def test_satisfies_its_port(self, fake, port):
        assert isinstance(fake, port)


@pytest.mark.db
class TestFakePOS:
    def test_same_day_replays_identically(self, db):
        pos = FakePOS()
        first = pos.generate_day(DAY)
        second = pos.generate_day(DAY)
        assert [o.external_id for o in first] == [o.external_id for o in second]
        assert [o.subtotal for o in first] == [o.subtotal for o in second]

    def test_different_days_differ(self, db):
        pos = FakePOS()
        assert pos.generate_day(DAY) != pos.generate_day(DAY + timedelta(days=1))

    def test_busier_on_a_saturday_than_a_tuesday(self, db):
        pos = FakePOS()
        tuesday = date(2026, 8, 25)
        saturday = date(2026, 8, 29)
        assert tuesday.weekday() == 1 and saturday.weekday() == 5
        assert len(pos.generate_day(saturday)) > len(pos.generate_day(tuesday))

    def test_orders_are_chronological(self, db):
        orders = FakePOS().generate_day(DAY)
        assert orders == sorted(orders, key=lambda o: o.placed_at)

    def test_every_order_has_lines(self, db):
        assert all(o.lines for o in FakePOS().generate_day(DAY))

    def test_channel_mix_is_plausible(self, db):
        counts = Counter(o.channel for o in FakePOS().generate_day(DAY))
        assert counts["dine_in"] > counts.get("kiosk", 0)

    def test_delivery_orders_name_a_platform(self, db):
        for order in FakePOS().generate_day(DAY):
            if order.channel == "delivery":
                assert order.delivery_platform

    def test_cash_orders_have_no_processor_reference(self, db):
        for order in FakePOS().generate_day(DAY):
            if order.payment_method == "cash":
                assert order.processor_ref is None

    def test_produces_custom_dietary_requests(self, db):
        # The order agent's whole job is interpreting these, so the simulator
        # must actually emit them.
        modifiers = [
            line.modifiers
            for order in FakePOS().generate_day(DAY)
            for line in order.lines
            if line.modifiers
        ]
        assert modifiers
        assert any("allerg" in m or "no " in m for m in modifiers)

    def test_push_order_records_it(self, db):
        pos = FakePOS()
        order = PosOrder(
            external_id="X-1",
            channel="phone",
            placed_at=clock.now(),
            lines=[PosOrderLine(sku="MNU-KOPIO", quantity=1, unit_price=D("5.50"))],
        )
        assert pos.push_order(order) == "X-1"
        assert pos.pushed[0].external_id == "X-1"

    def test_fetch_orders_respects_the_window(self, db):
        pos = FakePOS()
        everything = pos.generate_day(DAY)
        midpoint = everything[len(everything) // 2].placed_at
        window = pos.fetch_orders(midpoint, everything[-1].placed_at)
        assert all(o.placed_at >= midpoint for o in window)
        assert len(window) < len(everything)


class TestFakeReviews:
    def test_deterministic(self):
        reviews = FakeReviews()
        assert [r.external_id for r in reviews.generate_day(DAY)] == [
            r.external_id for r in reviews.generate_day(DAY)
        ]

    def test_produces_reviews_needing_escalation(self):
        # Over a long window the low-star tail must actually appear, or the
        # escalation path is never tested.
        reviews = FakeReviews()
        sample = [r for d in range(120) for r in reviews.generate_day(DAY - timedelta(days=d))]
        low = [r for r in sample if r.rating <= 2]
        assert len(sample) > 100
        assert 0.10 < len(low) / len(sample) < 0.30, "expected roughly a fifth to be poor"

    def test_ratings_are_in_range(self):
        reviews = FakeReviews()
        sample = [r for d in range(30) for r in reviews.generate_day(DAY - timedelta(days=d))]
        assert all(1 <= r.rating <= 5 for r in sample)

    def test_low_ratings_carry_negative_text(self):
        reviews = FakeReviews()
        sample = [r for d in range(60) for r in reviews.generate_day(DAY - timedelta(days=d))]
        for review in sample:
            if review.rating <= 2:
                assert len(review.body) > 30

    def test_publish_response_is_recorded(self):
        reviews = FakeReviews()
        ref = reviews.publish_response("REV-1", "Thank you for the feedback.")
        assert ref.endswith("REV-1")
        assert reviews.published["REV-1"].startswith("Thank you")


class TestFakeSupplier:
    def _place(self, supplier: FakeSupplier, count: int) -> dict[str, Decimal]:
        prices = {"SKU-A": D("50.00"), "SKU-B": D("30.00")}
        for index in range(count):
            supplier.send_purchase_order(
                f"PO-T-{index:04d}", "SUP-DRY", [("SKU-A", D("4")), ("SKU-B", D("3"))], prices
            )
        return prices

    def test_acknowledges_an_order(self):
        supplier = FakeSupplier()
        assert supplier.send_purchase_order("PO-1", "SUP-DRY", [("SKU-A", D("2"))]) == "ACK-PO-1"

    def test_does_not_deliver_before_the_lead_time(self):
        supplier = FakeSupplier(lead_days=3)
        supplier.send_purchase_order("PO-1", "SUP-DRY", [("SKU-A", D("2"))])
        assert supplier.fetch_deliveries(clock.now()) == []

    def test_delivers_once_the_lead_time_elapses(self):
        supplier = FakeSupplier(lead_days=0)
        supplier.send_purchase_order("PO-1", "SUP-DRY", [("SKU-A", D("2"))])
        assert len(supplier.fetch_deliveries(clock.now())) == 1

    def test_delivers_each_order_only_once(self):
        supplier = FakeSupplier(lead_days=0)
        supplier.send_purchase_order("PO-1", "SUP-DRY", [("SKU-A", D("2"))])
        supplier.fetch_deliveries(clock.now())
        assert supplier.fetch_deliveries(clock.now()) == []

    def test_sometimes_short_delivers(self):
        supplier = FakeSupplier(lead_days=0)
        self._place(supplier, 60)
        notes = supplier.fetch_deliveries(clock.now())
        ordered = {"SKU-A": D("4"), "SKU-B": D("3")}
        lines = [(sku, packs) for note in notes for sku, packs in note.lines]
        short = [1 for sku, packs in lines if packs < ordered[sku]]
        assert 0.05 < len(short) / len(lines) < 0.40, "shorts must occur, but not always"

    def test_short_deliveries_are_annotated(self):
        supplier = FakeSupplier(lead_days=0)
        self._place(supplier, 60)
        notes = supplier.fetch_deliveries(clock.now())
        assert any(note.note and "Short on" in note.note for note in notes)

    def test_invoices_only_what_was_delivered(self):
        supplier = FakeSupplier(lead_days=0)
        supplier.send_purchase_order("PO-1", "SUP-DRY", [("SKU-A", D("2"))])
        assert supplier.fetch_invoices(clock.now()) == [], "must deliver before invoicing"
        supplier.fetch_deliveries(clock.now())
        assert len(supplier.fetch_invoices(clock.now())) == 1

    def test_sometimes_charges_above_contract(self):
        supplier = FakeSupplier(lead_days=0)
        prices = self._place(supplier, 60)
        supplier.fetch_deliveries(clock.now())
        invoices = supplier.fetch_invoices(clock.now())
        lines = [(sku, price) for inv in invoices for sku, _packs, price in inv.lines]
        over = [1 for sku, price in lines if price > prices[sku]]
        assert 0.05 < len(over) / len(lines) < 0.40, "price drift must occur, but not always"

    def test_price_drift_is_a_creep_not_a_leap(self):
        # Small unnoticed drifts are what erode margin; a doubled price would
        # be caught by anyone.
        supplier = FakeSupplier(lead_days=0)
        prices = self._place(supplier, 60)
        supplier.fetch_deliveries(clock.now())
        for invoice in supplier.fetch_invoices(clock.now()):
            for sku, _packs, price in invoice.lines:
                assert price <= prices[sku] * D("1.20")

    def test_invoice_totals_are_consistent(self):
        supplier = FakeSupplier(lead_days=0)
        self._place(supplier, 5)
        supplier.fetch_deliveries(clock.now())
        for invoice in supplier.fetch_invoices(clock.now()):
            expected = sum(packs * price for _sku, packs, price in invoice.lines)
            assert invoice.subtotal == expected
            assert invoice.total == invoice.subtotal + invoice.tax

    def test_the_same_order_always_behaves_the_same(self):
        def run() -> list[tuple[str, Decimal]]:
            supplier = FakeSupplier(lead_days=0)
            supplier.send_purchase_order(
                "PO-STABLE", "SUP-DRY", [("SKU-A", D("4"))], {"SKU-A": D("50.00")}
            )
            notes = supplier.fetch_deliveries(clock.now())
            return [(sku, packs) for note in notes for sku, packs in note.lines]

        assert run() == run()


class TestFakeSocial:
    def test_records_a_scheduled_post(self):
        social = FakeSocial()
        post = ScheduledPost(
            platform="instagram", body="Nasi lemak today", scheduled_for=clock.now()
        )
        ref = social.schedule_post(post)
        assert ref.startswith("SOC-")
        assert social.fetch_scheduled()[0].external_ref == ref


class TestFakeBank:
    def test_commission_rates(self):
        assert FakeBank.commission_for("GrabFood", D("1000")) == D("300.00")
        assert FakeBank.commission_for("foodpanda", D("1000")) == D("280.00")

    def test_unknown_platform_gets_a_default_rate(self):
        assert FakeBank.commission_for("SomeNewApp", D("1000")) > D("0")

    def test_card_fee(self):
        assert FakeBank.card_fee(D("100")) == D("1.80")

    @pytest.mark.db
    def test_settlements_lag_the_business_date(self, db):
        for line in FakeBank(settle_lag_days=1).fetch_settlements(date(2026, 8, 20)):
            assert line.posted_on > date(2026, 8, 20)


class TestFakeMessaging:
    def test_deterministic(self):
        messaging = FakeMessaging()
        assert [m.external_id for m in messaging.generate_day(DAY)] == [
            m.external_id for m in messaging.generate_day(DAY)
        ]

    def test_produces_free_text_requests(self):
        messages = FakeMessaging().generate_day(DAY)
        assert messages
        assert all(len(m.body) > 20 for m in messages)

    def test_send_is_recorded(self):
        messaging = FakeMessaging()
        messaging.send_message("+60122334455", "Confirmed for 4 at 19:30.")
        assert messaging.sent[0][0] == "+60122334455"
