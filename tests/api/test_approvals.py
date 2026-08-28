"""The approval flow, end to end.

The claim under test: an agent proposes, stops, and the proposal is durable
enough that a completely separate process — the Slack webhook handler — can
resolve it later and let the agent finish. Nothing irreversible happens in
between.
"""

from __future__ import annotations

import json
import urllib.parse

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from restaurant_ai.api.main import app
from restaurant_ai.approvals.service import expire_stale, list_pending, resolve
from restaurant_ai.approvals.slack import build_blocks, parse_interaction
from restaurant_ai.approvals.telegram import build_message, parse_callback
from restaurant_ai.db.models import (
    ApprovalRequest,
    ApprovalStatus,
    PurchaseOrder,
    PurchaseOrderStatus,
)
from restaurant_ai.kernel.registry import get_agent
from restaurant_ai.kernel.runner import run_agent

pytestmark = pytest.mark.db


API_KEY = "test-approval-key"
TELEGRAM_SECRET = "test-telegram-secret"
SLACK_SECRET = "test-slack-secret"


@pytest.fixture
def credentials(monkeypatch):
    """Configure the secrets the approval endpoints now require.

    They fail closed: unset means the endpoint refuses to serve, not that it
    serves anyone. So a test that wants to reach one has to say who it is.
    """
    from restaurant_ai.config import reset_settings_cache

    monkeypatch.setenv("APPROVAL_API_KEY", API_KEY)
    monkeypatch.setenv("TELEGRAM_WEBHOOK_SECRET", TELEGRAM_SECRET)
    monkeypatch.setenv("SLACK_SIGNING_SECRET", SLACK_SECRET)
    reset_settings_cache()
    yield
    reset_settings_cache()


@pytest.fixture
def client(db, credentials):
    with TestClient(app) as c:
        c.headers.update({"X-API-Key": API_KEY})
        yield c


@pytest.fixture
def anonymous(db):
    """A caller with no credentials at all — the internet."""
    with TestClient(app) as c:
        yield c


@pytest.fixture
def parked(db, stock_is_low):
    """A stock_reorder run parked at its approval gate."""
    outcome = run_agent(get_agent("stock_reorder"), trigger="test")
    assert outcome.interrupted, (
        "stock is below its reorder point, so the agent must propose a purchase order"
    )
    return outcome


class TestRequestRecording:
    def test_a_parked_run_leaves_a_resolvable_request(self, db, parked):
        db.expire_all()
        requests = list(
            db.execute(
                select(ApprovalRequest).where(ApprovalRequest.run_id == parked.run_id)
            ).scalars()
        )
        assert requests, "the request must be written before the interrupt unwinds the graph"
        request = requests[0]
        assert request.status == ApprovalStatus.PENDING
        assert request.thread_id == parked.thread_id
        assert request.value > 0

    def test_the_request_stands_on_its_own(self, db, parked):
        # Whoever opens Slack must be able to judge this without the database.
        db.expire_all()
        request = (
            db.execute(select(ApprovalRequest).where(ApprovalRequest.run_id == parked.run_id))
            .scalars()
            .first()
        )
        assert "purchase order" in request.title.lower()
        assert "on hand" in request.detail
        assert "reorder point" in request.detail

    def test_it_carries_an_expiry(self, db, parked):
        db.expire_all()
        request = (
            db.execute(select(ApprovalRequest).where(ApprovalRequest.run_id == parked.run_id))
            .scalars()
            .first()
        )
        assert request.expires_at is not None
        assert request.expires_at > request.requested_at

    def test_nothing_is_sent_before_approval(self, db, parked):
        db.expire_all()
        sent = (
            db.execute(
                select(PurchaseOrder).where(
                    PurchaseOrder.created_by_run_id == parked.run_id,
                    PurchaseOrder.status == PurchaseOrderStatus.SENT,
                )
            )
            .scalars()
            .all()
        )
        assert sent == [], "the supplier must not be contacted before a human agrees"


class TestResolution:
    def test_approving_completes_the_run_and_sends_the_order(self, db, parked):
        request_id = _pending_id(db, parked.run_id)
        outcome = resolve(request_id, approved=True, resolved_by="aishah", note="fine")

        assert outcome["approved"] is True
        db.expire_all()
        sent = (
            db.execute(
                select(PurchaseOrder).where(
                    PurchaseOrder.created_by_run_id == parked.run_id,
                    PurchaseOrder.status == PurchaseOrderStatus.SENT,
                )
            )
            .scalars()
            .all()
        )
        assert sent, "approval must actually transmit the order"
        assert all(o.approved_by == "aishah" for o in sent), "the approver must be recorded"

    def test_rejecting_sends_nothing(self, db, parked):
        request_id = _pending_id(db, parked.run_id)
        resolve(request_id, approved=False, resolved_by="manager", note="too much cash")

        db.expire_all()
        sent = (
            db.execute(
                select(PurchaseOrder).where(
                    PurchaseOrder.created_by_run_id == parked.run_id,
                    PurchaseOrder.status == PurchaseOrderStatus.SENT,
                )
            )
            .scalars()
            .all()
        )
        assert sent == []

    def test_the_decision_is_recorded(self, db, parked):
        request_id = _pending_id(db, parked.run_id)
        resolve(request_id, approved=True, resolved_by="aishah", note="checked with chef")

        db.expire_all()
        request = db.get(ApprovalRequest, request_id)
        assert request.status == ApprovalStatus.APPROVED
        assert request.resolved_by == "aishah"
        assert request.resolution_note == "checked with chef"
        assert request.resolved_at is not None

    def test_resolving_twice_is_refused(self, db, parked):
        # A second approval would resume a finished graph and could re-send the
        # purchase order.
        request_id = _pending_id(db, parked.run_id)
        resolve(request_id, approved=True, resolved_by="aishah")
        with pytest.raises(ValueError, match="already approved"):
            resolve(request_id, approved=True, resolved_by="someone-else")

    def test_unknown_request(self, db):
        with pytest.raises(KeyError):
            resolve("no-such-id", approved=True, resolved_by="x")

    def test_pending_listing(self, db, parked):
        pending = list_pending()
        assert any(p["run_id"] == parked.run_id for p in pending)

    def test_expiry_closes_the_request_without_committing(self, db, parked):
        from restaurant_ai import clock

        request_id = _pending_id(db, parked.run_id)
        request = db.get(ApprovalRequest, request_id)
        request.expires_at = clock.utcnow().replace(year=2020)
        db.flush()

        assert expire_stale() >= 1
        db.expire_all()
        assert db.get(ApprovalRequest, request_id).status == ApprovalStatus.EXPIRED
        sent = (
            db.execute(
                select(PurchaseOrder).where(
                    PurchaseOrder.created_by_run_id == parked.run_id,
                    PurchaseOrder.status == PurchaseOrderStatus.SENT,
                )
            )
            .scalars()
            .all()
        )
        assert sent == [], "an expired request must never commit"


def _slack_headers(raw: bytes) -> dict[str, str]:
    """Sign a body the way Slack does, so the endpoint accepts it."""
    import hashlib
    import hmac
    import time

    timestamp = str(int(time.time()))
    basestring = f"v0:{timestamp}:".encode() + raw
    signature = "v0=" + hmac.new(SLACK_SECRET.encode(), basestring, hashlib.sha256).hexdigest()
    return {"X-Slack-Request-Timestamp": timestamp, "X-Slack-Signature": signature}


class TestApiEndpoints:
    def test_list_pending(self, client, db, parked):
        body = client.get("/approvals").json()
        assert body["count"] >= 1
        assert any(p["run_id"] == parked.run_id for p in body["pending"])

    def test_resolve_via_api(self, client, db, parked):
        request_id = _pending_id(db, parked.run_id)
        response = client.post(
            f"/approvals/{request_id}/resolve",
            json={"approved": True, "resolved_by": "api-user"},
        )
        assert response.status_code == 200
        assert response.json()["approved"] is True

    def test_resolve_unknown_returns_404(self, client, db):
        response = client.post(
            "/approvals/no-such-id/resolve", json={"approved": True, "resolved_by": "x"}
        )
        assert response.status_code == 404

    def test_double_resolve_returns_409(self, client, db, parked):
        request_id = _pending_id(db, parked.run_id)
        client.post(f"/approvals/{request_id}/resolve", json={"approved": True, "resolved_by": "a"})
        second = client.post(
            f"/approvals/{request_id}/resolve", json={"approved": True, "resolved_by": "b"}
        )
        assert second.status_code == 409

    def test_slack_button_resolves(self, client, db, parked):
        request_id = _pending_id(db, parked.run_id)
        payload = {
            "actions": [{"action_id": "approve", "value": request_id}],
            "user": {"username": "aishah"},
        }
        raw = urllib.parse.urlencode({"payload": json.dumps(payload)}).encode()
        response = client.post(
            "/approvals/slack/interactivity",
            content=raw,
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                **_slack_headers(raw),
            },
        )
        assert response.status_code == 200
        assert "Approved by aishah" in response.json()["text"]

        db.expire_all()
        assert db.get(ApprovalRequest, request_id).status == ApprovalStatus.APPROVED

    def test_telegram_callback_resolves(self, client, db, parked):
        request_id = _pending_id(db, parked.run_id)
        response = client.post(
            "/approvals/telegram/callback",
            json={
                "callback_query": {
                    "data": f"no:{request_id}",
                    "from": {"username": "manager"},
                }
            },
            headers={"X-Telegram-Bot-Api-Secret-Token": TELEGRAM_SECRET},
        )
        assert response.json()["ok"] is True
        db.expire_all()
        assert db.get(ApprovalRequest, request_id).status == ApprovalStatus.REJECTED


class TestCardRendering:
    REQUEST = {
        "id": "abc-123",
        "agent_name": "stock_reorder",
        "title": "3 purchase orders totalling 4963.80",
        "detail": "Hock Seng Dry Goods - 4093.80\n    Coconut milk: 10 packs",
        "value": "4963.80",
    }

    def test_slack_card_has_both_buttons(self):
        blocks = build_blocks(self.REQUEST)
        actions = blocks[-1]["elements"]
        assert [a["action_id"] for a in actions] == ["approve", "reject"]

    def test_slack_approve_requires_confirmation(self):
        # Committing money should take one deliberate extra click.
        approve = build_blocks(self.REQUEST)[-1]["elements"][0]
        assert "confirm" in approve
        assert "4,963.80" in approve["confirm"]["text"]["text"]

    def test_slack_truncates_an_oversized_detail(self):
        # Slack rejects a section over 3000 characters outright.
        blocks = build_blocks({**self.REQUEST, "detail": "x" * 9000})
        section = next(b for b in blocks if b.get("text", {}).get("text", "").startswith("```"))
        assert len(section["text"]["text"]) < 3000

    def test_slack_interaction_parsing(self):
        payload = {
            "actions": [{"action_id": "reject", "value": "abc-123"}],
            "user": {"username": "manager"},
        }
        body = urllib.parse.urlencode({"payload": json.dumps(payload)}).encode()
        interaction = parse_interaction(body)
        assert interaction.approval_id == "abc-123"
        assert interaction.approved is False
        assert interaction.user == "manager"

    def test_slack_ignores_unrelated_interactions(self):
        payload = {"actions": [{"action_id": "open_modal", "value": "x"}], "user": {}}
        body = urllib.parse.urlencode({"payload": json.dumps(payload)}).encode()
        assert parse_interaction(body) is None

    def test_slack_handles_garbage(self):
        assert parse_interaction(b"not a form") is None

    def test_telegram_callback_data_fits_the_limit(self):
        # Telegram caps callback_data at 64 bytes; a truncated id would resolve
        # the wrong request.
        _text, keyboard = build_message(self.REQUEST)
        for button in keyboard["inline_keyboard"][0]:
            assert len(button["callback_data"].encode()) <= 64

    def test_telegram_parsing(self):
        interaction = parse_callback(
            {"callback_query": {"data": "ok:abc-123", "from": {"username": "aishah"}}}
        )
        assert interaction.approval_id == "abc-123" and interaction.approved is True

    def test_telegram_ignores_non_callbacks(self):
        assert parse_callback({"message": {"text": "hello"}}) is None

    def test_telegram_ignores_malformed_data(self):
        assert parse_callback({"callback_query": {"data": "garbage", "from": {}}}) is None


def _pending_id(db, run_id: str) -> str:
    db.expire_all()
    request = (
        db.execute(
            select(ApprovalRequest).where(
                ApprovalRequest.run_id == run_id, ApprovalRequest.status == ApprovalStatus.PENDING
            )
        )
        .scalars()
        .first()
    )
    assert request is not None, "expected a pending approval"
    return request.id


class TestTheDoorIsShut:
    """The approval gate is worth what the endpoint recording the answer is worth.

    It was worth nothing. `GET /approvals` listed every pending request with its
    id and `POST /approvals/telegram/callback` resolved any id handed to it,
    both unauthenticated — so anything that could reach the port could approve a
    purchase order and sign someone else's name to it. Demonstrated, not
    theorised: eleven pending approvals listed, then one approved as
    "not-the-owner", with no credentials at all.
    """

    def test_a_forged_telegram_callback_is_refused(self, anonymous, db, parked, credentials):
        request_id = _pending_id(db, parked.run_id)
        response = anonymous.post(
            "/approvals/telegram/callback",
            json={"callback_query": {"data": f"ok:{request_id}", "from": {"username": "mallory"}}},
        )
        assert response.status_code == 401
        db.expire_all()
        assert db.get(ApprovalRequest, request_id).status == ApprovalStatus.PENDING

    def test_a_wrong_secret_is_refused(self, anonymous, db, parked, credentials):
        request_id = _pending_id(db, parked.run_id)
        response = anonymous.post(
            "/approvals/telegram/callback",
            json={"callback_query": {"data": f"ok:{request_id}", "from": {"username": "mallory"}}},
            headers={"X-Telegram-Bot-Api-Secret-Token": "not-the-secret"},
        )
        assert response.status_code == 401
        db.expire_all()
        assert db.get(ApprovalRequest, request_id).status == ApprovalStatus.PENDING

    def test_the_pending_list_needs_a_key(self, anonymous, credentials):
        # It hands out the ids the callback endpoint acts on.
        assert anonymous.get("/approvals").status_code == 401

    def test_resolving_needs_a_key(self, anonymous, db, parked, credentials):
        request_id = _pending_id(db, parked.run_id)
        response = anonymous.post(
            f"/approvals/{request_id}/resolve",
            json={"approved": True, "resolved_by": "mallory"},
        )
        assert response.status_code == 401
        db.expire_all()
        assert db.get(ApprovalRequest, request_id).status == ApprovalStatus.PENDING

    def test_running_an_agent_needs_a_key(self, anonymous, credentials):
        assert anonymous.post("/agents/stock_reorder/run", json={}).status_code == 401

    def test_an_unsigned_slack_callback_is_refused(self, anonymous, db, parked, credentials):
        request_id = _pending_id(db, parked.run_id)
        payload = {
            "actions": [{"action_id": "approve", "value": request_id}],
            "user": {"username": "mallory"},
        }
        response = anonymous.post(
            "/approvals/slack/interactivity",
            content=urllib.parse.urlencode({"payload": json.dumps(payload)}).encode(),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        assert response.status_code == 401


class TestAMissingSecretClosesTheDoor:
    """Unset must mean shut, not open.

    Slack's verifier used to return early when no signing secret was set, on the
    reasoning that this was local development. In a deployment that missed the
    line in .env it meant anyone could approve anything, silently.
    """

    def test_no_api_key_configured_refuses_rather_than_serves(self, anonymous):
        response = anonymous.get("/approvals")
        assert response.status_code == 503
        assert "APPROVAL_API_KEY" in response.json()["detail"]

    def test_no_telegram_secret_configured_refuses(self, anonymous):
        response = anonymous.post(
            "/approvals/telegram/callback",
            json={"callback_query": {"data": "ok:x", "from": {"username": "mallory"}}},
        )
        assert response.status_code == 503
        assert "TELEGRAM_WEBHOOK_SECRET" in response.json()["detail"]

    def test_no_slack_secret_configured_refuses(self, anonymous):
        response = anonymous.post(
            "/approvals/slack/interactivity",
            content=b"payload=%7B%7D",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        assert response.status_code == 503

    def test_health_stays_open(self, anonymous):
        # Whatever is watching the service still has to be able to watch it.
        assert anonymous.get("/health").status_code == 200
