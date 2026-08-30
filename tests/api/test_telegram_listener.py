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

    def test_an_update_with_no_chat_to_answer_is_ignored(self, db, telegram):
        """Nowhere to send a reply is not the same as an unauthorised asker."""
        assert listener.handle_update({"update_id": 5, "message": {"text": "hello"}}) is None
        assert not [p for m, p in telegram if m == "sendMessage"]


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


def say(text: str, *, chat: str = CHAT, user: int = 42) -> dict:
    """A typed message in the approvals chat."""
    return {
        "update_id": 2,
        "message": {
            "message_id": 11,
            "text": text,
            "from": {"id": user, "username": "sharif"},
            "chat": {"id": int(chat)},
        },
    }


class TestTheOwnerCanAsk:
    """The other half of the conversation: a message is a question."""

    def test_a_question_is_answered_into_the_same_chat(self, db, telegram, monkeypatch):
        monkeypatch.setattr(
            "restaurant_ai.assistant.answer",
            lambda q, session=None, history=None: f"answering: {q}",
        )
        described = listener.handle_update(say("how much chicken do we have?"))

        assert described.startswith("answered:")
        sent = [payload for method, payload in telegram if method == "sendMessage"]
        assert len(sent) == 1
        assert sent[0]["chat_id"] == int(CHAT)
        assert sent[0]["text"] == "answering: how much chicken do we have?"

    def test_help_explains_both_asking_and_telling(self, db, telegram):
        listener.handle_update(say("/help"))
        text = [p for m, p in telegram if m == "sendMessage"][0]["text"]
        assert "ASK ME" in text and "TELL ME" in text
        assert "/brief" in text and "/pending" in text and "/run" in text
        # It must never leave the owner thinking an instruction is a done deed.
        assert "not the same as it being done" in text

    def test_start_is_help_too(self, db, telegram):
        """The first thing anyone types to a new bot is /start."""
        listener.handle_update(say("/start"))
        assert "ASK ME" in [p for m, p in telegram if m == "sendMessage"][0]["text"]

    def test_brief_sends_tonights_brief_on_demand(self, db, telegram):
        listener.handle_update(say("/brief"))
        text = [p for m, p in telegram if m == "sendMessage"][0]["text"]
        assert "daily brief" in text
        assert "MONEY" in text

    def test_pending_lists_what_is_waiting_by_name(self, db, telegram, parked):
        listener.handle_update(say("/pending"))
        text = [p for m, p in telegram if m == "sendMessage"][0]["text"]
        assert "waiting for your approval" in text
        assert "Rain" in text

    def test_a_quiet_queue_says_nothing_is_waiting(self, db, telegram):
        from restaurant_ai.db.models import ApprovalRequest

        for row in db.query(ApprovalRequest).all():
            db.delete(row)
        db.flush()

        listener.handle_update(say("/pending"))
        assert "Nothing is waiting" in [p for m, p in telegram if m == "sendMessage"][0]["text"]

    def test_a_command_addressed_to_the_bot_by_name_still_works(self, db, telegram):
        """In a group, Telegram sends "/brief@Keanu007_Bot"."""
        listener.handle_update(say("/brief@Keanu007_Bot"))
        assert "daily brief" in [p for m, p in telegram if m == "sendMessage"][0]["text"]

    def test_a_sticker_is_not_a_question(self, db, telegram):
        assert (
            listener.handle_update({"update_id": 3, "message": {"chat": {"id": int(CHAT)}}}) is None
        )
        assert not [p for m, p in telegram if m == "sendMessage"]


class TestWhoMayAsk:
    """The numbers are the restaurant's. The allow-list guards them."""

    def test_a_stranger_gets_no_answer(self, db, telegram, monkeypatch):
        monkeypatch.setattr(
            "restaurant_ai.assistant.answer",
            lambda q, session=None, history=None: "should never be called",
        )
        with pytest.raises(listener.UnauthorisedPresser):
            listener.handle_update(say("what are today's takings?", chat="111", user=222))

        # Silence, not a refusal: a reply would confirm the bot and the chat.
        assert not [p for m, p in telegram if m == "sendMessage"]

    def test_a_stranger_does_not_block_the_queue(self, db, telegram, monkeypatch):
        monkeypatch.setattr(
            listener,
            "api",
            lambda method, **kw: (
                {"ok": True, "result": [say("hi", chat="111", user=222)]}
                if method == "getUpdates"
                else {"ok": True, "result": {}}
            ),
        )
        offset, handled = listener.poll_once(None)
        assert offset == 3
        assert handled and handled[0].startswith("ignored:")

    def test_the_listener_now_asks_telegram_for_messages_too(self, db, telegram, monkeypatch):
        asked = {}

        def fake_api(method, **payload):
            asked.update(payload)
            return {"ok": True, "result": []}

        monkeypatch.setattr(listener, "api", fake_api)
        listener.poll_once(None)
        assert asked["allowed_updates"] == ["callback_query", "message"]


def press_run(agent: str, *, action: str = "run", chat: str = CHAT, user: int = 42) -> dict:
    """A press on a "Run X?" confirmation card."""
    return {
        "update_id": 4,
        "callback_query": {
            "id": "cbq-run",
            "data": f"{action}:{agent}",
            "from": {"id": user, "username": "sharif"},
            "message": {"message_id": 12, "chat": {"id": int(chat)}},
        },
    }


@pytest.fixture
def routes(monkeypatch):
    """Route every instruction to a chosen intent, without a model."""
    from restaurant_ai.assistant import Intent

    box = {"intent": Intent(kind="question")}
    monkeypatch.setattr("restaurant_ai.assistant.route", lambda text, history=None: box["intent"])
    monkeypatch.setattr(
        "restaurant_ai.assistant.answer", lambda q, session=None, history=None: f"answering: {q}"
    )
    return box


def _sent(calls) -> list[str]:
    return [p["text"] for m, p in calls if m == "sendMessage"]


class TestTakingAnInstruction:
    def test_an_instruction_proposes_the_agent_and_asks_first(self, db, telegram, routes):
        """A misroute must cost a wrong sentence, not a wrong run."""
        from restaurant_ai.assistant import Intent

        routes["intent"] = Intent(kind="run", agent="stock_reorder")
        described = listener.handle_update(say("restock the kitchen"))

        assert described == "proposed: stock_reorder"
        card = [p for m, p in telegram if m == "sendMessage"][0]
        assert "Rain" in card["text"]
        buttons = card["reply_markup"]["inline_keyboard"][0]
        assert buttons[0]["callback_data"] == "run:stock_reorder"
        assert buttons[1]["callback_data"] == "drop:stock_reorder"
        # Nothing has run.
        assert "on it" not in " ".join(_sent(telegram))

    def test_confirming_runs_the_agent_and_reports_back(self, db, telegram, stock_is_low):
        described = listener.handle_update(press_run("stock_reorder"))

        assert described.startswith("ran stock_reorder")
        sent = _sent(telegram)
        assert "Rain is on it…" in sent[0]
        # The outcome is reported, never assumed.
        assert any("Rain:" in line for line in sent[1:])

    def test_a_parked_run_says_it_still_needs_approving(self, db, telegram, stock_is_low):
        """Telling me to do it is not the same as it being done."""
        listener.handle_update(press_run("stock_reorder"))
        assert any("needs your approval" in line for line in _sent(telegram))

    def test_declining_runs_nothing(self, db, telegram):
        described = listener.handle_update(press_run("stock_reorder", action="drop"))
        assert described == "declined: stock_reorder"
        assert "nothing run" in " ".join(_sent(telegram))

    def test_the_owners_own_words_reach_the_agent(self, db, telegram, routes, monkeypatch):
        from restaurant_ai.assistant import Intent

        seen = {}

        def spy(spec, **kwargs):
            seen.update(kwargs)

            class Outcome:
                summary = "done"
                interrupted = False

            return Outcome()

        monkeypatch.setattr("restaurant_ai.kernel.runner.run_agent", spy)
        routes["intent"] = Intent(kind="run", agent="stock_reorder")
        listener.handle_update(say("order extra prawns for the weekend"))
        listener.handle_update(press_run("stock_reorder"))

        assert seen["trigger"] == "telegram"
        assert seen["trigger_payload"] == {"instruction": "order extra prawns for the weekend"}

    def test_an_unclear_instruction_asks_rather_than_guessing(self, db, telegram, routes):
        from restaurant_ai.assistant import Intent

        routes["intent"] = Intent(kind="unclear", reason="I could not tell.")
        assert listener.handle_update(say("do the thing")) == "unclear"
        text = _sent(telegram)[0]
        assert "I could not tell." in text
        # It always names the way through.
        assert "/run rain" in text

    def test_a_question_still_gets_an_answer(self, db, telegram, routes):
        listener.handle_update(say("how much chicken?"))
        assert _sent(telegram) == ["answering: how much chicken?"]


class TestTheDeterministicPath:
    """/run cannot be misrouted: no model is consulted."""

    def test_run_by_person_name_works(self, db, telegram, stock_is_low):
        assert listener.handle_update(say("/run rain")).startswith("ran stock_reorder")

    def test_run_by_slug_works(self, db, telegram, stock_is_low):
        assert listener.handle_update(say("/run stock_reorder")).startswith("ran stock_reorder")

    def test_an_unknown_name_is_refused_by_name(self, db, telegram):
        described = listener.handle_update(say("/run gordon"))
        assert described == "run: unknown agent gordon"
        assert "nobody called" in _sent(telegram)[0]
        assert "/agents" in _sent(telegram)[0]

    def test_run_with_nobody_named_asks_who(self, db, telegram):
        listener.handle_update(say("/run"))
        assert "Run whom?" in _sent(telegram)[0]

    def test_agents_lists_everyone_with_the_command_to_run_them(self, db, telegram):
        listener.handle_update(say("/agents"))
        text = _sent(telegram)[0]
        assert "/run stock_reorder" in text and "Rain" in text
        # One runnable line per agent, plus the header that explains the form.
        assert text.count("\n  /run ") == 11

    def test_an_unknown_command_says_so_rather_than_guessing(self, db, telegram):
        listener.handle_update(say("/frobnicate"))
        assert "I do not know /frobnicate" in _sent(telegram)[0]


class TestItNeverGoesQuiet:
    def test_a_failure_is_reported_into_the_chat(self, db, telegram, monkeypatch):
        """Silence reads as success. It must not be the answer to a crash."""
        monkeypatch.setattr(
            listener,
            "api",
            lambda method, **kw: (
                {"ok": True, "result": [say("how are we?")]}
                if method == "getUpdates"
                else telegram.append((method, kw)) or {"ok": True, "result": {}}
            ),
        )
        monkeypatch.setattr(
            "restaurant_ai.assistant.route",
            lambda text, history=None: (_ for _ in ()).throw(RuntimeError("the model is on fire")),
        )

        offset, handled = listener.poll_once(None)

        assert offset == 3
        assert handled[0].startswith("failed:")
        told = _sent(telegram)
        assert told and "the model is on fire" in told[0]
        assert "Nothing was changed." in told[0]

    def test_a_stranger_gets_no_apology_either(self, db, telegram, monkeypatch):
        monkeypatch.setattr(
            listener,
            "api",
            lambda method, **kw: (
                {"ok": True, "result": [say("hi", chat="111", user=222)]}
                if method == "getUpdates"
                else telegram.append((method, kw)) or {"ok": True, "result": {}}
            ),
        )
        listener.poll_once(None)
        assert not _sent(telegram)

    def test_an_agent_that_throws_is_reported_not_swallowed(self, db, telegram, monkeypatch):
        monkeypatch.setattr(
            "restaurant_ai.kernel.runner.run_agent",
            lambda spec, **kw: (_ for _ in ()).throw(RuntimeError("postgres went away")),
        )
        described = listener.handle_update(press_run("stock_reorder"))
        assert described.startswith("failed:")
        assert "postgres went away" in _sent(telegram)[-1]


class TestWhoMayInstruct:
    def test_a_stranger_cannot_run_an_agent(self, db, telegram, monkeypatch):
        monkeypatch.setattr(
            "restaurant_ai.kernel.runner.run_agent",
            lambda spec, **kw: pytest.fail("a stranger must never start an agent"),
        )
        with pytest.raises(listener.UnauthorisedPresser):
            listener.handle_update(press_run("stock_reorder", chat="111", user=222))


class TestLoggingWhatSold:
    """`/sold` — the way real trading gets in without a POS."""

    @pytest.fixture
    def real_menu(self, db):
        from restaurant_ai.db.catalog_import import import_catalog

        import_catalog(
            db, "menu/the-great-invention-menu.xlsx", allow_uncosted=True, replace_menu=True
        )
        return db

    def test_it_reads_the_day_back_in_money_before_writing(self, real_menu, telegram):
        described = listener.handle_update(say("/sold 20 nasi lemak biasa, 35 teh tarik"))

        assert described.startswith("sold: proposed")
        card = [p for m, p in telegram if m == "sendMessage"][0]
        assert "Nasi Lemak Biasa" in card["text"]
        # 20 nasi lemak biasa at 4.00 + 35 teh tarik at 2.50, per the printed menu.
        assert "RM 167.50" in card["text"]
        assert card["reply_markup"]["inline_keyboard"][0][0]["callback_data"] == "sold:go"

    def test_nothing_is_written_until_the_press(self, real_menu, telegram):
        from restaurant_ai import demo

        before = demo.real_orders(real_menu)
        listener.handle_update(say("/sold 20 nasi lemak biasa"))
        assert demo.real_orders(real_menu) == before

    def test_the_press_records_it_as_real_trading(self, real_menu, telegram):
        from restaurant_ai import demo

        before = demo.real_orders(real_menu)
        listener.handle_update(say("/sold 20 nasi lemak biasa"))
        described = listener.handle_update(press_run("go", action="sold"))

        assert described.startswith("recorded takings")
        assert demo.real_orders(real_menu) == before + 1
        assert any("Camelia will close" in p["text"] for m, p in telegram if m == "sendMessage")

    def test_declining_writes_nothing(self, real_menu, telegram):
        from restaurant_ai import demo

        before = demo.real_orders(real_menu)
        listener.handle_update(say("/sold 20 nasi lemak biasa"))
        listener.handle_update(press_run("go", action="nosold"))

        assert demo.real_orders(real_menu) == before
        assert any("nothing recorded" in p["text"] for m, p in telegram if m == "sendMessage")

    def test_an_ambiguous_dish_is_asked_about_not_guessed(self, real_menu, telegram):
        described = listener.handle_update(say("/sold 12 nasi lemak"))

        assert "unresolved" in described
        text = [p for m, p in telegram if m == "sendMessage"][0]["text"]
        assert "could be" in text
        assert "Nothing was written" in text

    def test_a_stranger_cannot_record_takings(self, real_menu, telegram):
        with pytest.raises(listener.UnauthorisedPresser):
            listener.handle_update(press_run("go", action="sold", chat="111", user=222))

    def test_a_forgotten_confirmation_asks_for_it_again_rather_than_guessing(
        self, real_menu, telegram
    ):
        """A restart between reading and pressing must cost a re-type, not a wrong write."""
        listener._PENDING_TAKINGS.clear()
        described = listener.handle_update(press_run("go", action="sold"))

        assert described == "takings: nothing pending"
        assert any("lost what that was" in p["text"] for m, p in telegram if m == "sendMessage")

    def test_sold_with_nothing_after_it_shows_the_shape(self, real_menu, telegram):
        listener.handle_update(say("/sold"))
        assert "nasi lemak" in [p for m, p in telegram if m == "sendMessage"][0]["text"]


class TestGreetingsAreAnsweredFree:
    def test_hey_gets_a_reply_without_a_model(self, db, telegram, monkeypatch):
        monkeypatch.setattr(
            "restaurant_ai.assistant.answer",
            lambda q, session=None, history=None: pytest.fail("a greeting must not reach the desk"),
        )
        described = listener.handle_update(say("hey"))

        assert described == "greeted"
        text = [p for m, p in telegram if m == "sendMessage"][0]["text"]
        assert "/agents" in text
