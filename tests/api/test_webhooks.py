"""Webhook ingestion.

A webhook endpoint is an unauthenticated door into the platform: whatever can
POST to /webhooks/pos can create sales, move stock and ultimately cause purchase
orders. These pin down that only correctly signed, non-replayed payloads get
through, and that a redelivery changes nothing.
"""

from __future__ import annotations

import json
import time
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from restaurant_ai.api.main import app
from restaurant_ai.api.security import sign_payload
from restaurant_ai.db.models import InboundEvent, Ingredient, OrderHeader, OrderLine, StockMovement

pytestmark = pytest.mark.db


@pytest.fixture
def client(db):
    with TestClient(app) as c:
        yield c


@pytest.fixture(autouse=True)
def _inline(monkeypatch):
    """Run handlers inline rather than queueing them.

    The endpoint queues to Celery when a broker is reachable. Redis is up in
    this environment, so without this the work would go to a queue no test
    worker is draining and every assertion about the outcome would be racing.
    """
    monkeypatch.setattr(
        "restaurant_ai.worker.celery_app.broker_available", lambda timeout=1.0: False
    )


def _signed(payload: dict) -> tuple[bytes, dict[str, str]]:
    body = json.dumps(payload).encode()
    timestamp = str(int(time.time()))
    return body, {
        "X-Signature": sign_payload(body, timestamp),
        "X-Timestamp": timestamp,
        "Content-Type": "application/json",
    }


def _order_payload(**overrides) -> dict:
    payload = {
        "event_id": "EVT-1",
        "external_id": "POS-1",
        "order_number": "W-1",
        "channel": "dine_in",
        "party_size": 2,
        "placed_at": "2026-08-27T19:15:00+08:00",
        "payment_method": "card",
        "processor_ref": "TXN-1",
        "lines": [{"sku": "MNU-NASILEMK", "quantity": 2}],
    }
    payload.update(overrides)
    return payload


class TestSignature:
    def test_valid_signature_is_accepted(self, client):
        body, headers = _signed(_order_payload())
        assert client.post("/webhooks/pos", content=body, headers=headers).status_code == 202

    def test_unsigned_is_rejected(self, client):
        body, _ = _signed(_order_payload())
        response = client.post(
            "/webhooks/pos", content=body, headers={"Content-Type": "application/json"}
        )
        assert response.status_code == 401

    def test_tampered_body_is_rejected(self, client):
        _body, headers = _signed(_order_payload())
        response = client.post("/webhooks/pos", content=b'{"event_id":"evil"}', headers=headers)
        assert response.status_code == 401
        assert "Signature does not match" in response.json()["detail"]

    def test_stale_timestamp_is_rejected(self, client):
        # A valid signature captured off the wire must not stay valid forever.
        payload = _order_payload()
        body = json.dumps(payload).encode()
        old = str(int(time.time()) - 10_000)
        headers = {
            "X-Signature": sign_payload(body, old),
            "X-Timestamp": old,
            "Content-Type": "application/json",
        }
        response = client.post("/webhooks/pos", content=body, headers=headers)
        assert response.status_code == 401
        assert "replay" in response.json()["detail"]

    def test_signature_from_a_different_secret_is_rejected(self, client):
        payload = _order_payload()
        body = json.dumps(payload).encode()
        timestamp = str(int(time.time()))
        headers = {
            "X-Signature": sign_payload(body, timestamp, secret="not-the-real-secret"),
            "X-Timestamp": timestamp,
            "Content-Type": "application/json",
        }
        assert client.post("/webhooks/pos", content=body, headers=headers).status_code == 401


class TestIdempotency:
    def test_replay_creates_one_order(self, client, db):
        body, headers = _signed(_order_payload(event_id="EVT-DUP", external_id="POS-DUP"))
        first = client.post("/webhooks/pos", content=body, headers=headers)
        second = client.post("/webhooks/pos", content=body, headers=headers)

        assert first.json()["status"] == "accepted"
        assert second.json()["status"] == "duplicate"

        db.expire_all()
        count = db.execute(
            select(func.count(OrderHeader.id)).where(OrderHeader.external_ref == "POS-DUP")
        ).scalar_one()
        assert count == 1

    def test_replay_does_not_deduct_stock_twice(self, client, db):
        chicken = db.execute(
            select(Ingredient).where(Ingredient.code == "ING-CHKN-THI")
        ).scalar_one()

        def balance() -> Decimal:
            db.expire_all()
            return Decimal(
                str(
                    db.execute(
                        select(func.coalesce(func.sum(StockMovement.quantity), 0)).where(
                            StockMovement.ingredient_id == chicken.id
                        )
                    ).scalar_one()
                )
            )

        body, headers = _signed(_order_payload(event_id="EVT-S", external_id="POS-S"))
        before = balance()
        client.post("/webhooks/pos", content=body, headers=headers)
        after_first = balance()
        client.post("/webhooks/pos", content=body, headers=headers)
        after_second = balance()

        assert before - after_first == Decimal("360.0000"), "2 x 180g chicken"
        assert after_first == after_second, "a replayed webhook must not deduct again"

    def test_payload_without_an_id_is_rejected(self, client):
        body, headers = _signed({"channel": "dine_in", "lines": []})
        response = client.post("/webhooks/pos", content=body, headers=headers)
        assert response.status_code == 400
        assert "event_id" in response.json()["detail"]

    def test_malformed_json_is_rejected(self, client):
        timestamp = str(int(time.time()))
        body = b"{not json"
        headers = {
            "X-Signature": sign_payload(body, timestamp),
            "X-Timestamp": timestamp,
            "Content-Type": "application/json",
        }
        assert client.post("/webhooks/pos", content=body, headers=headers).status_code == 400

    def test_the_raw_payload_is_retained(self, client, db):
        body, headers = _signed(_order_payload(event_id="EVT-KEEP", external_id="POS-KEEP"))
        client.post("/webhooks/pos", content=body, headers=headers)
        db.expire_all()
        event = db.execute(
            select(InboundEvent).where(InboundEvent.external_id == "EVT-KEEP")
        ).scalar_one()
        assert event.payload["external_id"] == "POS-KEEP"
        assert event.provider == "pos"


class TestStockDeduction:
    def test_a_sale_explodes_through_the_recipe(self, client, db):
        # Belacan is only reachable via the sambal sub-recipe, so seeing it move
        # proves the deduction walked the full BOM rather than the top level.
        belacan = db.execute(
            select(Ingredient).where(Ingredient.code == "ING-BLC-SHR")
        ).scalar_one()
        body, headers = _signed(_order_payload(event_id="EVT-B", external_id="POS-B"))
        client.post("/webhooks/pos", content=body, headers=headers)

        db.expire_all()
        order = db.execute(
            select(OrderHeader).where(OrderHeader.external_ref == "POS-B")
        ).scalar_one()
        # Scoped to this order: the database may hold movements from other
        # trading, and this is asserting what THIS sale deducted.
        movements = list(
            db.execute(
                select(StockMovement).where(
                    StockMovement.ingredient_id == belacan.id,
                    StockMovement.source_type == "order",
                    StockMovement.source_id == order.id,
                )
            ).scalars()
        )
        assert movements, "belacan should be deducted via the sambal sub-recipe"
        assert sum(m.quantity for m in movements) == Decimal("-4.0000"), "2 x 2g"

    def test_deductions_are_negative(self, client, db):
        body, headers = _signed(_order_payload(event_id="EVT-N", external_id="POS-N"))
        client.post("/webhooks/pos", content=body, headers=headers)
        db.expire_all()
        order = db.execute(
            select(OrderHeader).where(OrderHeader.external_ref == "POS-N")
        ).scalar_one()
        movements = list(
            db.execute(
                select(StockMovement).where(
                    StockMovement.source_type == "order", StockMovement.source_id == order.id
                )
            ).scalars()
        )
        assert movements and all(m.quantity < 0 for m in movements)

    def test_order_totals_include_tax(self, client, db):
        body, headers = _signed(_order_payload(event_id="EVT-T", external_id="POS-T"))
        client.post("/webhooks/pos", content=body, headers=headers)
        db.expire_all()
        order = db.execute(
            select(OrderHeader).where(OrderHeader.external_ref == "POS-T")
        ).scalar_one()
        assert order.tax > 0
        assert order.total == order.subtotal + order.tax

    def test_unknown_sku_is_skipped_not_fatal(self, client, db):
        payload = _order_payload(
            event_id="EVT-U",
            external_id="POS-U",
            lines=[
                {"sku": "MNU-DOES-NOT-EXIST", "quantity": 1},
                {"sku": "MNU-KOPIO", "quantity": 1},
            ],
        )
        body, headers = _signed(payload)
        assert client.post("/webhooks/pos", content=body, headers=headers).status_code == 202
        db.expire_all()
        order = db.execute(
            select(OrderHeader).where(OrderHeader.external_ref == "POS-U")
        ).scalar_one()
        lines = db.execute(
            select(func.count(OrderLine.id)).where(OrderLine.order_id == order.id)
        ).scalar_one()
        assert lines == 1, "the known line still goes through"


class TestOtherEndpoints:
    def test_review_webhook(self, client, db):
        from restaurant_ai.db.models import Review

        payload = {
            "event_id": "REV-EVT-1",
            "external_id": "REV-1",
            "platform": "google",
            "author": "Test Guest",
            "rating": 2,
            "body": "Waited far too long and nobody checked on us.",
            "posted_at": "2026-08-27T20:00:00+08:00",
        }
        body, headers = _signed(payload)
        assert client.post("/webhooks/reviews", content=body, headers=headers).status_code == 202
        db.expire_all()
        assert db.execute(select(Review).where(Review.external_id == "REV-1")).scalar_one_or_none()

    def test_delivery_payout_webhook(self, client, db):
        from restaurant_ai.db.models import DeliveryPayout

        payload = {
            "event_id": "PAY-EVT-1",
            "payout_ref": "GRAB-W35",
            "platform": "GrabFood",
            "period_start": "2026-08-17",
            "period_end": "2026-08-23",
            "gross_sales": "4200.00",
            "commission": "1260.00",
        }
        body, headers = _signed(payload)
        assert client.post("/webhooks/delivery", content=body, headers=headers).status_code == 202
        db.expire_all()
        payout = db.execute(
            select(DeliveryPayout).where(DeliveryPayout.payout_ref == "GRAB-W35")
        ).scalar_one()
        assert payout.net_payout == Decimal("2940.00")


class TestHealth:
    def test_health(self, client):
        body = client.get("/health").json()
        assert body["status"] == "ok"
        assert body["llm"]["provider"] == "fake"

    def test_ready_checks_dependencies(self, client):
        body = client.get("/ready").json()
        assert "postgres" in body["checks"] and "redis" in body["checks"]

    def test_root(self, client):
        assert client.get("/").json()["service"] == "restaurant-ai"
