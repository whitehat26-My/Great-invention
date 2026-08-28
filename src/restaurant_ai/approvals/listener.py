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

    if "callback_query" in update:
        data = (update["callback_query"] or {}).get("data", "")
        if data.startswith(("run:", "drop:")):
            return handle_run_press(update["callback_query"])

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


_HELP = """I run this restaurant with you. Two things you can do:

ASK ME
  "how much chicken do we have?"
  "what are today's numbers?"
  "who is on tomorrow?"

TELL ME
  "restock the kitchen"
  "build next week's roster"
  "close the day"
  I work out whose job it is and ask you to confirm before anything runs.

COMMANDS
  /run <name>  run that agent now, no guessing  (e.g. /run rain)
  /agents      who does what
  /brief       tonight's brief, right now
  /pending     what is waiting for your approval
  /help        this message

Nothing that spends money or changes a price happens without a card you
approve — telling me to do it is not the same as it being done."""


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
    elif command == "/agents":
        reply = _agents_text()
    elif command == "/run":
        return _run_command(chat_id, text)
    elif command:
        reply = f"I do not know {command}. Try /help."
    else:
        return _instruction_or_question(chat_id, text)

    api("sendMessage", chat_id=chat_id, text=reply)
    return f"answered: {text[:60]}"


def _instruction_or_question(chat_id: Any, text: str) -> str:
    """Work out whether the owner asked something or told me to do something."""
    from restaurant_ai.assistant import Intent, answer, route

    intent: Intent = route(text)

    if intent.kind == "run" and intent.agent:
        _propose_run(chat_id, intent.agent, text)
        return f"proposed: {intent.agent}"

    if intent.kind == "unclear":
        api(
            "sendMessage",
            chat_id=chat_id,
            text=(
                f"{intent.reason or 'I could not tell what you meant.'}\n\n"
                "Ask me a question, or name the agent outright — /run rain, "
                "/run henry. /agents lists them."
            ),
        )
        return "unclear"

    api("sendMessage", chat_id=chat_id, text=answer(text))
    return f"answered: {text[:60]}"


def _propose_run(chat_id: Any, agent: str, instruction: str) -> None:
    """Say what would run, and ask first.

    Routing is a judgement, and the owner finds out which agent was picked only
    after it has run. Confirming turns a misroute into a wrong sentence on the
    screen instead of a wrong agent in the audit trail.
    """
    from restaurant_ai.kernel.registry import get_agent

    spec = get_agent(agent)
    _REMEMBERED[agent] = instruction
    api(
        "sendMessage",
        chat_id=chat_id,
        text=(
            f"That is {spec.person}'s job — {spec.title}.\n\n"
            f"{spec.description}\n\n"
            f"Run {spec.person} now?"
        ),
        reply_markup={
            "inline_keyboard": [
                [
                    {"text": f"Run {spec.person}", "callback_data": f"run:{agent}"},
                    {"text": "No", "callback_data": f"drop:{agent}"},
                ]
            ]
        },
    )


# What the owner actually said, kept only between proposing a run and the press
# that confirms it, so the agent is told why it was woken. Losing it (a restart
# between the two) costs context, never correctness — the agent's job is defined
# by its own spec, not by this.
_REMEMBERED: dict[str, str] = {}


def handle_run_press(query: dict[str, Any]) -> str:
    """A press on a "Run X?" card. Same allow-list as every other press."""
    message = query.get("message") or {}
    chat_id = (message.get("chat") or {}).get("id")
    user_id = (query.get("from") or {}).get("id")

    if not permitted(chat_id, user_id):
        log.warning("ignored a run press from outside the allow-list", chat=chat_id, user=user_id)
        answer_callback(query.get("id", ""), "You are not authorised to run this.")
        raise UnauthorisedPresser(f"chat={chat_id} user={user_id}")

    action, _, agent = (query.get("data") or "").partition(":")
    if action == "drop":
        _REMEMBERED.pop(agent, None)
        answer_callback(query.get("id", ""), "Left alone.")
        api("sendMessage", chat_id=chat_id, text="Fine — nothing run.")
        return f"declined: {agent}"

    answer_callback(query.get("id", ""), "Running.")
    return _run_now(chat_id, agent, _REMEMBERED.pop(agent, ""))


def _run_command(chat_id: Any, text: str) -> str:
    """``/run rain`` — the path that cannot be misrouted.

    No model decides anything here. This is what the owner falls back to when
    the router is unsure, and what still works when it cannot be reached at all.
    """
    from restaurant_ai.assistant import find_agent

    wanted = text.split(maxsplit=1)[1].strip() if " " in text else ""
    if not wanted:
        api("sendMessage", chat_id=chat_id, text="Run whom? Try /run rain. /agents lists them.")
        return "run: no agent named"

    agent = find_agent(wanted)
    if agent is None:
        api(
            "sendMessage",
            chat_id=chat_id,
            text=f"I have nobody called “{wanted}”. /agents lists them.",
        )
        return f"run: unknown agent {wanted}"

    return _run_now(chat_id, agent, "")


def _run_now(chat_id: Any, agent: str, instruction: str) -> str:
    """Run one agent and report what actually happened.

    Every path out of here sends the owner a message. A run that fails, parks or
    does nothing at all is reported as that — silence would read as success.
    """
    from restaurant_ai.kernel.registry import get_agent
    from restaurant_ai.kernel.runner import run_agent

    spec = get_agent(agent)
    api("sendMessage", chat_id=chat_id, text=f"{spec.person} is on it…")

    try:
        outcome = run_agent(
            spec,
            trigger="telegram",
            trigger_payload={"instruction": instruction} if instruction else None,
        )
    except Exception as exc:
        log.error("agent run failed", agent=agent, error=str(exc))
        api(
            "sendMessage",
            chat_id=chat_id,
            text=f"{spec.person} could not finish: {type(exc).__name__}: {exc}",
        )
        return f"failed: {agent}: {exc}"

    summary = (outcome.summary or "").strip() or "nothing to report"
    if outcome.interrupted:
        text = f"{spec.person}: {summary}\n\nThe card above needs your approval before it happens."
    else:
        text = f"{spec.person}: {summary}"
    api("sendMessage", chat_id=chat_id, text=text[:3800])
    return f"ran {agent}{' (parked)' if outcome.interrupted else ''}"


def _agents_text() -> str:
    from restaurant_ai.kernel.registry import all_agents

    lines = ["Who works here — /run <name> puts any of them to work now:", ""]
    for name, spec in sorted(all_agents().items(), key=lambda kv: kv[1].department):
        lines.append(f"{spec.person} — {spec.title}")
        lines.append(f"  /run {name}")
    return "\n".join(lines)


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
            _apologise(update, exc)
            continue
        if described:
            handled.append(described)
    return offset, handled


def _apologise(update: dict[str, Any], exc: Exception) -> None:
    """Tell the owner the thing they asked for did not happen.

    Everything above this line is caught so one bad update cannot stop the
    listener. That protects the process and abandons the person: they typed
    something, and nothing came back — which reads exactly like being ignored,
    or worse, like it worked. If the reply itself cannot be sent there is
    genuinely nothing left to do but log it.
    """
    message = update.get("message") or (update.get("callback_query") or {}).get("message") or {}
    chat_id = (message.get("chat") or {}).get("id")
    # The chat is the allow-list, and an apology to a stranger would confirm the
    # bot exists as surely as an answer would.
    if chat_id is None or not permitted(chat_id, None):
        return
    try:
        api(
            "sendMessage",
            chat_id=chat_id,
            text=f"That did not work: {type(exc).__name__}: {exc}\n\nNothing was changed.",
        )
    except Exception as reply_failure:  # pragma: no cover - the chat itself is down
        log.error("could not report the failure", error=str(reply_failure))


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
