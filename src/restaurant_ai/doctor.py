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


def _read_env_file(path: Any) -> dict[str, str]:
    """The file's own view of the settings, for comparison against the live ones.

    Deliberately not a dotenv parser: no interpolation, no export, no multi-line
    values. It exists to answer "does the environment disagree with the file",
    and a line it cannot read is a line it should not have an opinion about.
    """
    found: dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return found
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, _, value = line.partition("=")
        name = name.strip()
        value = value.strip().strip("\"'")
        if name.isidentifier():
            found[name.upper()] = value
    return found


def _check_configuration(report: Diagnosis) -> None:
    """Which .env is being read, and does it say what the owner thinks it says?

    Three ways an edit to .env silently does nothing, all of them seen in the
    wild, none of them producing an error:

    - ``env_file=".env"`` resolves against the *working directory*, not the
      project folder. Run from one folder up and a different file is read, or
      none, and every setting falls back to its default.
    - A real environment variable **outranks the file**. Once ``LLM_PROVIDER``
      is set in the shell, the file is read and then overridden for the life of
      that window, and editing it will never help.
    - Windows hides known extensions, so "save as .env" in Notepad produces
      ``.env.txt``, which looks right in Explorer and is invisible to the loader.

    The provider mismatch below is the fourth. Putting ANTHROPIC_API_KEY in .env
    and leaving LLM_PROVIDER=google is the natural halfway point of a switch, it
    looks finished, and the only sign is a provider name in a log line nobody
    reads.
    """
    import os
    from pathlib import Path

    settings = get_settings()
    here = Path.cwd()
    env = here / ".env"
    if env.exists():
        report.add("settings file", True, f"reading {env}")
    else:
        # Say what *is* there before saying what is not. A near miss is the
        # likeliest explanation and the one hardest to see in Explorer.
        near = sorted(
            p.name for p in here.glob(".env*") if p.is_file() and p.name not in (".env.example",)
        )
        detail = f"no .env in {here} — every setting is at its default"
        fix = (
            "Commands read `.env` from the folder you run them in. Change to the project "
            "folder first, or copy .env there."
        )
        if near:
            detail += f"; found {', '.join(near)}"
            fix = (
                f"{', '.join(near)} is not read — the file must be named exactly `.env`. "
                "Windows hides known extensions, so a file saved from Notepad is usually "
                "`.env.txt`. Rename it with: Rename-Item .env.txt .env"
            )
        report.add("settings file", False, detail, fix)

    # An environment variable outranking the file is normal — compose passes
    # every setting that way. It is only a fault when the two *disagree*, which
    # is the case where editing the file changes nothing and says nothing.
    disagreeing = []
    if env.exists():
        for key, value in _read_env_file(env).items():
            live = os.environ.get(key)
            if live is not None and live != value:
                # doctor output gets pasted into chats and issues. A secret is
                # named, never quoted — on either side of the disagreement.
                secret = "KEY" in key or "TOKEN" in key
                shown = key if secret else f"{key}={live} (the file says {value})"
                disagreeing.append(shown)
    if disagreeing:
        report.add(
            "environment",
            False,
            f"the environment overrides .env: {', '.join(disagreeing)}",
            "A shell variable outranks the file, so editing .env cannot change these. "
            "Close this terminal and open a new one, then start again from the project "
            "folder.",
        )

    keys = {"anthropic": settings.anthropic_api_key, "google": settings.google_api_key}
    unused = [name for name, key in keys.items() if key and name != settings.llm_provider]
    if unused:
        report.add(
            "provider",
            False,
            f"LLM_PROVIDER={settings.llm_provider}, but a key for {', '.join(unused)} is also set",
            f"A key alone changes nothing — the provider chooses. Set "
            f"LLM_PROVIDER={unused[0]} in .env and restart, or remove the unused key.",
        )


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
            # `make` is a developer's tool and this is the owner's message: on the
            # machine where the database is actually unreachable there is no make.
            "Start it with `restaurant-ai up`, which starts Postgres and applies the "
            "schema itself.",
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
    """Ask each configured model something, because configured is not working.

    A key that is right and a free tier that is spent look identical from the
    settings; an Ollama host in .env looks identical whether or not anything is
    listening on it. Only a real call tells them apart.

    When a deployment splits the providers, *both* are called. Checking only the
    one the owner talks to leaves the machine doing four fifths of the work
    untested, and that is precisely the half whose failure is silent — the chat
    keeps answering while every scheduled agent quietly stops thinking.
    """
    from restaurant_ai.kernel import llm

    described = llm.describe_provider()
    split = bool(described.get("interactive_provider"))

    # (label, interactive) — one entry unless the deployment splits them.
    calls: list[tuple[str, bool]] = (
        [("language model (agents)", False), ("language model (chat)", True)]
        if split
        else [("language model", True)]
    )

    for label, interactive in calls:
        if llm.is_fake(interactive=interactive):
            report.add(
                label,
                True,
                "fake — agents run their code, but nothing reasons",
                "Set LLM_PROVIDER and the matching API key in .env to use a real model.",
            )
            continue

        provider = llm.provider_for(interactive=interactive)
        name = f"{provider} — {llm.model_name('conversational', interactive=interactive)}"
        try:
            from langchain_core.messages import HumanMessage

            reply = llm.get_model("conversational", interactive=interactive).invoke(
                [HumanMessage(content="Reply with the single word: ok")]
            )
            del reply
            report.add(label, True, f"{name}, answering")
        except Exception as exc:
            from restaurant_ai.assistant import explain_model_failure

            report.add(label, False, f"{name} — {exc}", explain_model_failure(exc))


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
    _check_configuration(report)
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
