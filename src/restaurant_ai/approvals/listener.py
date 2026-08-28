"""Long-polling approval listener.

The webhook path needs a public HTTPS URL, which a restaurant that is still
being built does not have. This asks Telegram for updates instead, so approvals
work from a laptop behind a router with no hosting, no certificate and no DNS.

The trade is that something has to keep running. That is fine for a single
operator watching their own restaurant, and the webhook remains the right answer
once the service has an address.

Every decision lands on ``approvals.service.resolve``, the same function the
webhook and the CLI use — the listener authenticates the caller and does no
resolving of its own.

It carries both directions of the conversation. A button press decides an
approval; a typed message is a question, answered from the restaurant's own
state by ``assistant.answer``. Both go through the same allow-list: the bot
token is a bearer credential, so anyone who learns it can talk to the bot, and
only the configured chat may either decide or ask.
"""

from __future__ import annotations

import time
from typing import Any

from restaurant_ai.approvals.service import resolve
from restaurant_ai.approvals.telegram import (
    answer_callback,
    api,
    parse_callback,
    settle_message,
)
from restaurant_ai.config import get_settings
from restaurant_ai.logging_setup import get_logger

log = get_logger(__name__)

# Telegram holds the request open this long when there is nothing to report,
# which is what makes polling cheap rather than a busy loop.
POLL_SECONDS = 30


class UnauthorisedPresser(Exception):
    """Someone who is not on the allow-list pressed a button or asked a question."""


def permitted(chat_id: Any, user_id: Any) -> bool:
    """Whether this press counts.

    A bot token is a bearer credential: anyone who learns it can send to the
    bot, and any chat the bot is in can press its buttons. The configured chat
    is the allow-list — without this check, adding the bot to a group would give
    everyone in it authority to approve a purchase order.
    """
    configured = str(get_settings().telegram_chat_id or "")
    if not configured:
        return False
    return str(chat_id) == configured or str(user_id) == configured


def handle_update(update: dict[str, Any]) -> str | None:
    """Handle one update. Returns a short description, or None if not for us."""
    if "message" in update:
        return handle_message(update["message"])

    interaction = parse_callback(update)
    if interaction is None:
        return None

    query = update.get("callback_query") or {}
    message = query.get("message") or {}
    chat_id = (message.get("chat") or {}).get("id")
    user_id = (query.get("from") or {}).get("id")

    if not permitted(chat_id, user_id):
        log.warning("ignored a press from outside the allow-list", chat=chat_id, user=user_id)
        answer_callback(query.get("id", ""), "You are not authorised to decide this.")
        raise UnauthorisedPresser(f"chat={chat_id} user={user_id}")

    verdict = "Approved" if interaction.approved else "Rejected"
    try:
        outcome = resolve(
            interaction.approval_id,
            approved=interaction.approved,
            resolved_by=interaction.user,
            note=interaction.note,
            channel="telegram",
        )
    except (KeyError, ValueError) as exc:
        # Already decided, expired, or gone. Say so on the card rather than
        # leaving the button spinning.
        answer_callback(query.get("id", ""), f"Could not resolve: {exc}")
        return f"unresolved ({exc})"

    answer_callback(query.get("id", ""), f"{verdict}.")
    if chat_id and message.get("message_id"):
        settle_message(chat_id, message["message_id"], verdict, interaction.user)

    summary = str(outcome.get("summary", ""))[:120]
    return f"{verdict} by {interaction.user} — {summary}"


_HELP = """I am the question desk for this restaurant. Ask me anything:

  "how much chicken do we have?"
  "what are today's numbers?"
  "who is on tomorrow?"
  "why did Rain order rice?"

Commands:
  /brief    tonight's brief, right now
  /pending  what is waiting for your approval
  /help     this message

I can only read. Anything that changes the restaurant comes to you as a card to
approve."""


def handle_message(message: dict[str, Any]) -> str | None:
    """A typed message is a question. Answer it, or run a command.

    The allow-list is enforced here exactly as it is for a press. An unknown
    chat gets silence rather than an answer: replying would confirm the bot
    exists and hand a stranger the restaurant's numbers.
    """
    chat_id = (message.get("chat") or {}).get("id")
    user_id = (message.get("from") or {}).get("id")
    text = (message.get("text") or "").strip()

    if not text or chat_id is None:
        # A photo, a sticker, someone joining, or an update with no chat to
        # answer into: not a question, and nowhere to send a reply anyway.
        return None

    if not permitted(chat_id, user_id):
        log.warning("ignored a message from outside the allow-list", chat=chat_id, user=user_id)
        raise UnauthorisedPresser(f"chat={chat_id} user={user_id}")

    command = text.split()[0].lower().split("@")[0] if text.startswith("/") else ""

    if command in ("/help", "/start"):
        reply = _HELP
    elif command == "/brief":
        reply = _brief_text()
    elif command == "/pending":
        reply = _pending_text()
    else:
        from restaurant_ai.assistant import answer

        reply = answer(text)

    api("sendMessage", chat_id=chat_id, text=reply)
    return f"answered: {text[:60]}"


def _brief_text() -> str:
    from restaurant_ai.brief import build_brief, render_brief
    from restaurant_ai.db.base import session_scope

    with session_scope() as session:
        return render_brief(build_brief(session))


def _pending_text() -> str:
    from restaurant_ai.approvals.service import list_pending
    from restaurant_ai.kernel.registry import display_name

    pending = list_pending()
    if not pending:
        return "Nothing is waiting for you."
    lines = [f"{len(pending)} waiting for your approval:"]
    for item in pending:
        lines.append(
            f"  - {display_name(str(item['agent']))}: {item['title']} (value {item['value']})"
        )
    lines.append("")
    lines.append("Decide them on the cards in this chat.")
    return "\n".join(lines)


def poll_once(offset: int | None) -> tuple[int | None, list[str]]:
    """One getUpdates round. Returns the next offset and what was handled."""
    payload: dict[str, Any] = {
        "timeout": POLL_SECONDS,
        "allowed_updates": ["callback_query", "message"],
    }
    if offset is not None:
        payload["offset"] = offset

    updates = api("getUpdates", **payload)["result"]
    handled: list[str] = []
    for update in updates:
        # Advance past this update whatever happens to it. A poison update that
        # is never acknowledged is replayed forever, and the listener never sees
        # anything behind it again.
        offset = int(update["update_id"]) + 1
        try:
            described = handle_update(update)
        except UnauthorisedPresser as exc:
            handled.append(f"ignored: {exc}")
            continue
        except Exception as exc:
            log.error("update failed", error=str(exc))
            handled.append(f"failed: {type(exc).__name__}: {exc}")
            continue
        if described:
            handled.append(described)
    return offset, handled


def listen(on_event: Any = None, max_rounds: int | None = None) -> None:
    """Poll until interrupted, resolving approvals as they are decided."""
    from restaurant_ai.approvals.telegram import describe_bot

    described = describe_bot()
    if described["webhook_url"]:
        raise RuntimeError(
            f"A webhook is registered ({described['webhook_url']}), so getUpdates will "
            "refuse. Delete it first (deleteWebhook), or run the webhook path instead."
        )

    log.info("telegram listener started", bot=described["username"])
    offset: int | None = None
    rounds = 0
    while max_rounds is None or rounds < max_rounds:
        rounds += 1
        try:
            offset, handled = poll_once(offset)
        except Exception as exc:
            # A network blip must not end the shift. Back off and keep going.
            log.warning("poll failed, retrying", error=str(exc))
            time.sleep(5)
            continue
        for line in handled:
            log.info("approval handled", detail=line)
            if on_event is not None:
                on_event(line)
