"""The long-polling approval listener.

This is the path that works without hosting anything, which makes it the one a
restaurant still being built will actually use — and the one where a mistake is
least likely to be noticed, because there is no server log to read.
"""

from __future__ import annotations

from typing import Any

import pytest

from restaurant_ai.approvals import listener
from restaurant_ai.config import reset_settings_cache
from restaurant_ai.db.models import ApprovalRequest, ApprovalStatus
from restaurant_ai.kernel.registry import get_agent
from restaurant_ai.kernel.runner import run_agent

pytestmark = pytest.mark.db

CHAT = "998877"


@pytest.fixture
def telegram(monkeypatch):
    """Configure the bot, and capture every call that would hit the network."""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", CHAT)
    monkeypatch.setenv("APPROVAL_CHANNEL", "none")
    reset_settings_cache()

    calls: list[tuple[str, dict[str, Any]]] = []

    def fake_api(method: str, **payload: Any) -> dict[str, Any]:
        calls.append((method, payload))
        return {"ok": True, "result": {}}

    monkeypatch.setattr(listener, "api", fake_api)
    monkeypatch.setattr(
        listener, "answer_callback", lambda qid, text: calls.append(("answer", {"text": text}))
    )
    monkeypatch.setattr(
        listener, "settle_message", lambda c, m, v, w: calls.append(("settle", {"verdict": v}))
    )
    yield calls
    reset_settings_cache()


@pytest.fixture
def parked(db, stock_is_low):
    outcome = run_agent(get_agent("stock_reorder"), trigger="test")
    assert outcome.interrupted
    return outcome


def _pending_id(db, run_id: str) -> str:
    from sqlalchemy import select

    return db.execute(
        select(ApprovalRequest.id).where(ApprovalRequest.run_id == run_id)
    ).scalar_one()


def press(approval_id: str, *, approve: bool = True, chat: str = CHAT, user: int = 42) -> dict:
    verdict = "ok" if approve else "no"
    return {
        "update_id": 1,
        "callback_query": {
            "id": "cbq-1",
            "data": f"{verdict}:{approval_id}",
            "from": {"id": user, "username": "sharif"},
            "message": {"message_id": 7, "chat": {"id": int(chat)}},
        },
    }


class TestDecisions:
    def test_an_approval_from_the_configured_chat_resolves(self, db, telegram, parked):
        request_id = _pending_id(db, parked.run_id)
        described = listener.handle_update(press(request_id))

        assert described and "Approved by sharif" in described
        db.expire_all()
        assert db.get(ApprovalRequest, request_id).status == ApprovalStatus.APPROVED

    def test_a_rejection_resolves_too(self, db, telegram, parked):
        request_id = _pending_id(db, parked.run_id)
        listener.handle_update(press(request_id, approve=False))
        db.expire_all()
        assert db.get(ApprovalRequest, request_id).status == ApprovalStatus.REJECTED

    def test_the_button_is_always_answered(self, db, telegram, parked):
        """Telegram spins the button until answerCallbackQuery is called.

        Without it a decision that worked looks like one that hung, and the
        operator presses again.
        """
        listener.handle_update(press(_pending_id(db, parked.run_id)))
        assert any(method == "answer" for method, _ in telegram)

    def test_the_card_is_settled_so_it_cannot_be_pressed_twice(self, db, telegram, parked):
        listener.handle_update(press(_pending_id(db, parked.run_id)))
        settles = [payload for method, payload in telegram if method == "settle"]
        assert settles and settles[0]["verdict"] == "Approved"

    def test_an_already_decided_request_says_so_rather_than_hanging(self, db, telegram, parked):
        request_id = _pending_id(db, parked.run_id)
        listener.handle_update(press(request_id))
        described = listener.handle_update(press(request_id))
        assert described and "unresolved" in described

    def test_a_non_approval_update_is_ignored(self, db, telegram):
        assert listener.handle_update({"update_id": 5, "message": {"text": "hello"}}) is None


class TestWhoMayDecide:
    """A bot token is a bearer credential, and any chat the bot joins can press
    its buttons. The configured chat is the allow-list; without it, adding the
    bot to a group hands everyone in it authority over a purchase order."""

    def test_a_press_from_another_chat_is_refused(self, db, telegram, parked):
        request_id = _pending_id(db, parked.run_id)
        with pytest.raises(listener.UnauthorisedPresser):
            listener.handle_update(press(request_id, chat="112233", user=999))

        db.expire_all()
        assert db.get(ApprovalRequest, request_id).status == ApprovalStatus.PENDING

    def test_the_presser_is_told_why(self, db, telegram, parked):
        with pytest.raises(listener.UnauthorisedPresser):
            listener.handle_update(press(_pending_id(db, parked.run_id), chat="112233", user=999))
        answers = [p["text"] for m, p in telegram if m == "answer"]
        assert answers and "not authorised" in answers[0]

    def test_no_configured_chat_permits_nobody(self, monkeypatch):
        monkeypatch.setenv("TELEGRAM_CHAT_ID", "")
        reset_settings_cache()
        assert listener.permitted("anything", "anyone") is False
        reset_settings_cache()


class TestPolling:
    def test_the_offset_advances_past_a_handled_update(self, db, telegram, parked, monkeypatch):
        request_id = _pending_id(db, parked.run_id)
        update = press(request_id)
        update["update_id"] = 4100

        monkeypatch.setattr(listener, "api", lambda method, **kw: {"ok": True, "result": [update]})
        offset, handled = listener.poll_once(None)

        assert offset == 4101, "an unacknowledged update is replayed forever"
        assert handled and "Approved" in handled[0]

    def test_a_poison_update_still_advances_the_offset(self, db, telegram, monkeypatch):
        """Otherwise one bad update blocks every decision behind it, permanently."""
        broken = press("no-such-approval-id")
        broken["update_id"] = 77

        monkeypatch.setattr(listener, "api", lambda method, **kw: {"ok": True, "result": [broken]})
        offset, handled = listener.poll_once(None)
        assert offset == 78
        assert handled

    def test_an_unauthorised_press_does_not_block_the_queue(
        self, db, telegram, parked, monkeypatch
    ):
        intruder = press(_pending_id(db, parked.run_id), chat="112233", user=999)
        intruder["update_id"] = 500

        monkeypatch.setattr(
            listener, "api", lambda method, **kw: {"ok": True, "result": [intruder]}
        )
        offset, handled = listener.poll_once(None)
        assert offset == 501
        assert handled and "ignored" in handled[0]

    def test_it_refuses_to_poll_while_a_webhook_is_registered(self, telegram, monkeypatch):
        """getUpdates errors when a webhook exists, so say that rather than hang."""
        monkeypatch.setattr(
            "restaurant_ai.approvals.telegram.describe_bot",
            lambda: {"username": "b", "webhook_url": "https://example.test/hook", "name": "B"},
        )
        with pytest.raises(RuntimeError, match="webhook is registered"):
            listener.listen(max_rounds=1)


class TestFailuresAreDistinguishable:
    """A blocked connection and a bad token need opposite fixes.

    An egress proxy refusing CONNECT answers "403 Forbidden", which reads
    exactly like Telegram rejecting a token unless something says otherwise.
    One is a network policy; the other is a new token from BotFather.
    """

    def test_a_blocked_connection_says_so(self, telegram, monkeypatch):
        import httpx

        from restaurant_ai.approvals import telegram as tg

        def blocked(*a, **kw):
            raise httpx.ProxyError("403 Forbidden")

        monkeypatch.setattr(tg.httpx, "post", blocked)
        with pytest.raises(tg.TelegramUnreachable, match="not a bad token"):
            tg.api("getMe")

    def test_a_refused_token_says_so(self, telegram, monkeypatch):
        from restaurant_ai.approvals import telegram as tg

        class Response:
            status_code = 401

            def json(self):
                return {"ok": False, "description": "Unauthorized"}

        monkeypatch.setattr(tg.httpx, "post", lambda *a, **kw: Response())
        with pytest.raises(tg.TelegramRejected, match="Unauthorized"):
            tg.api("getMe")

    def test_something_answering_that_is_not_telegram_says_so(self, telegram, monkeypatch):
        from restaurant_ai.approvals import telegram as tg

        class HtmlPage:
            status_code = 502

            def json(self):
                raise ValueError("not json")

        monkeypatch.setattr(tg.httpx, "post", lambda *a, **kw: HtmlPage())
        with pytest.raises(tg.TelegramUnreachable, match="instead of Telegram"):
            tg.api("getMe")


class TestTheChatIdIsCheckedBeforeItIsTrusted:
    """A chat id is a bare number with nothing self-validating about it.

    `telegram-check` printed `chat id 123456789` — a placeholder pasted
    literally out of a runbook — and only fell over at the send, with Telegram's
    "chat not found". The number looked configured because nothing had asked
    whether it existed.
    """

    def test_a_real_chat_resolves_to_a_name(self, telegram, monkeypatch):
        from restaurant_ai.approvals import telegram as tg

        monkeypatch.setattr(
            tg,
            "api",
            lambda method, **kw: {
                "ok": True,
                "result": {"id": 1013758071, "type": "private", "first_name": "mellow"},
            },
        )
        described = tg.describe_chat("1013758071")
        assert described == {"id": 1013758071, "type": "private", "name": "mellow"}

    def test_a_group_uses_its_title(self, telegram, monkeypatch):
        from restaurant_ai.approvals import telegram as tg

        monkeypatch.setattr(
            tg,
            "api",
            lambda method, **kw: {
                "ok": True,
                "result": {"id": -100, "type": "group", "title": "Kitchen approvals"},
            },
        )
        assert tg.describe_chat("-100")["name"] == "Kitchen approvals"

    def test_a_placeholder_id_is_refused_by_telegram(self, telegram, monkeypatch):
        from restaurant_ai.approvals import telegram as tg

        class NotFound:
            status_code = 400

            def json(self):
                return {"ok": False, "description": "Bad Request: chat not found"}

        monkeypatch.setattr(tg.httpx, "post", lambda *a, **kw: NotFound())
        with pytest.raises(tg.TelegramRejected, match="chat not found"):
            tg.describe_chat("123456789")

    def test_a_chat_with_no_name_at_all_still_resolves(self, telegram, monkeypatch):
        from restaurant_ai.approvals import telegram as tg

        monkeypatch.setattr(
            tg, "api", lambda method, **kw: {"ok": True, "result": {"id": 5, "type": "private"}}
        )
        assert tg.describe_chat("5")["name"] == "unnamed"
