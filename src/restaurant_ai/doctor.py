"""Why nothing happened.

The failure that costs the most is not a crash — a crash says something. It is
the message sent to the bot that is met with silence, because silence is what a
working system and a dead one both look like from a phone.

Every check here answers one question the owner cannot answer from the chat, and
answers it in the order that matters: the last one, "is anything actually
listening?", is the one that explains almost every quiet bot. Nothing here
changes anything, so it is always safe to run.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from restaurant_ai.config import get_settings
from restaurant_ai.logging_setup import get_logger

log = get_logger(__name__)


@dataclass
class Check:
    name: str
    ok: bool
    detail: str
    fix: str = ""


@dataclass
class Diagnosis:
    checks: list[Check] = field(default_factory=list)

    @property
    def healthy(self) -> bool:
        return all(check.ok for check in self.checks)

    def add(self, name: str, ok: bool, detail: str, fix: str = "") -> None:
        self.checks.append(Check(name=name, ok=ok, detail=detail, fix=fix))


def _check_database(report: Diagnosis) -> None:
    try:
        from sqlalchemy import func, select

        from restaurant_ai.db.base import session_scope
        from restaurant_ai.db.models import Ingredient

        with session_scope() as session:
            tracked = session.execute(select(func.count()).select_from(Ingredient)).scalar_one()
        report.add("database", True, f"reachable, {tracked} ingredients tracked")
    except Exception as exc:
        report.add(
            "database",
            False,
            f"{type(exc).__name__}: {exc}",
            "Start it with `make up`, then `make migrate` and `make seed`.",
        )


def _check_trading_data(report: Diagnosis) -> None:
    """Whose numbers are these? Not a fault — a fact worth stating."""
    try:
        from restaurant_ai import demo
        from restaurant_ai.db.base import session_scope

        with session_scope() as session:
            invented = demo.synthetic_orders(session)
            genuine = demo.real_orders(session)
    except Exception:
        return  # the database check above already reported this

    if not invented:
        report.add("trading data", True, f"{genuine} real order(s), no demo data")
        return
    report.add(
        "trading data",
        True,
        f"{invented} demo order(s) from `seed`, {genuine} real",
        "Every total includes the demo trading. `restaurant-ai reset-db --yes` then "
        "`restaurant-ai up` gives you an empty restaurant to put real data into.",
    )


def _check_model(report: Diagnosis) -> None:
    from restaurant_ai.kernel import llm

    described = llm.describe_provider()
    if described.get("provider") == "fake":
        report.add(
            "language model",
            True,
            "fake — agents run their code, but nothing reasons",
            "Set LLM_PROVIDER and the matching API key in .env to use a real model.",
        )
        return
    # Configured is not the same as working. A key that is right and a free tier
    # that is spent look identical from the settings, and only one of them can
    # answer a question — so ask it something and see.
    name = f"{described.get('provider')} — {described.get('conversational', '?')}"
    try:
        from langchain_core.messages import HumanMessage

        reply = llm.get_model("conversational", interactive=True).invoke(
            [HumanMessage(content="Reply with the single word: ok")]
        )
        del reply
        report.add("language model", True, f"{name}, answering")
    except Exception as exc:
        from restaurant_ai.assistant import explain_model_failure

        report.add("language model", False, f"{name} — {exc}", explain_model_failure(exc))


def _check_telegram(report: Diagnosis) -> None:
    """The bot, the chat, and whether anything is reading the chat."""
    from restaurant_ai.approvals.telegram import (
        TelegramRejected,
        TelegramUnreachable,
        api,
        describe_bot,
        describe_chat,
    )

    settings = get_settings()
    if not settings.telegram_bot_token:
        report.add(
            "telegram bot",
            False,
            "TELEGRAM_BOT_TOKEN is not set",
            "Put the token from BotFather in .env as TELEGRAM_BOT_TOKEN.",
        )
        return

    try:
        described = describe_bot()
    except TelegramUnreachable as exc:
        report.add(
            "telegram bot",
            False,
            str(exc),
            "This is the network, not the token — try another network or a host that can "
            "reach api.telegram.org.",
        )
        return
    except TelegramRejected as exc:
        report.add(
            "telegram bot",
            False,
            str(exc),
            "The token is wrong or revoked. Get a fresh one from BotFather with /token.",
        )
        return

    report.add("telegram bot", True, f"@{described['username']} ({described['name']})")

    if described.get("webhook_url"):
        report.add(
            "webhook",
            False,
            f"a webhook is registered ({described['webhook_url']})",
            "Long polling refuses while a webhook is set. Delete it, or run the webhook path.",
        )
    else:
        report.add("webhook", True, "none — long polling is available")

    if not settings.telegram_chat_id:
        report.add(
            "approvals chat",
            False,
            "TELEGRAM_CHAT_ID is not set",
            "Message the bot, then run `restaurant-ai telegram-check` to learn the id.",
        )
    else:
        try:
            chat = describe_chat(settings.telegram_chat_id)
            report.add(
                "approvals chat",
                True,
                f"{chat['name']} ({chat['type']}, id {chat['id']})",
            )
        except Exception as exc:
            report.add(
                "approvals chat",
                False,
                f"TELEGRAM_CHAT_ID={settings.telegram_chat_id} does not resolve ({exc})",
                "Only this chat may ask, instruct or approve — everyone else is ignored in "
                "silence. Fix the id with `restaurant-ai telegram-check`.",
            )

    _check_listener(report, api)


def _check_listener(report: Diagnosis, api: Any) -> None:
    """Is anything actually reading the chat?

    Telegram allows exactly one ``getUpdates`` at a time and answers a second
    one with 409 Conflict. That makes the conflict the good news: something else
    is already polling, which is precisely what a running listener looks like
    from outside. An answer instead of a conflict means nothing is reading — and
    any updates it hands back are the messages that went unanswered.

    Called without an offset, so nothing is consumed: a real listener still sees
    everything after this runs.
    """
    from restaurant_ai.approvals.telegram import TelegramRejected, TelegramUnreachable

    try:
        result = api("getUpdates", timeout=0, limit=5)["result"]
    except TelegramRejected as exc:
        if "conflict" in str(exc).lower() or "terminated by other" in str(exc).lower():
            report.add("listener", True, "running — something is reading the chat")
        else:
            report.add("listener", False, str(exc))
        return
    except TelegramUnreachable as exc:
        report.add("listener", False, f"could not check ({exc})")
        return

    waiting = len(result)
    detail = "NOT RUNNING — nothing is reading the chat"
    if waiting:
        detail += f", and {waiting} message(s) are waiting unanswered"
    report.add(
        "listener",
        False,
        detail,
        "Start it with `restaurant-ai telegram-listen`, and leave it running — "
        "close that terminal and the bot goes deaf again.",
    )


def diagnose() -> Diagnosis:
    """Run every check. Never changes anything."""
    report = Diagnosis()
    _check_database(report)
    _check_trading_data(report)
    _check_model(report)
    _check_telegram(report)
    return report


def render(report: Diagnosis) -> str:
    settings = get_settings()
    width = max(len(check.name) for check in report.checks) if report.checks else 0
    lines = [f"{settings.restaurant_name} — what is and is not working", ""]
    for check in report.checks:
        # Providers answer a failed call with a page of JSON. The fix line below
        # is what the owner acts on; this line only has to identify the problem.
        detail = " ".join(check.detail.split())
        if len(detail) > 160:
            detail = detail[:160].rstrip() + "…"
        lines.append(f"  {'ok  ' if check.ok else 'FAIL'}  {check.name.ljust(width)}  {detail}")

    # A passing check can still carry advice — "these numbers are demo data" is
    # not a fault, and would be a lie as one, but the owner needs to read it.
    guidance = [check for check in report.checks if check.ok and check.fix]
    if guidance:
        lines.append("")
        for check in guidance:
            lines.append(f"  {check.name}: {check.fix}")

    broken = [check for check in report.checks if not check.ok and check.fix]
    if broken:
        lines.append("")
        for check in broken:
            lines.append(f"  {check.name}: {check.fix}")
    elif report.healthy:
        lines.append("")
        lines.append("  Everything is up. Message the bot and it will answer.")
    return "\n".join(lines)
