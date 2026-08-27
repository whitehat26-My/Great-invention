"""Telegram approval cards.

An inline keyboard with Approve and Reject. The approval id rides in the
callback data, which Telegram caps at 64 bytes — a UUID plus a short prefix
fits, which is why the callback carries the id rather than a serialised payload.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import httpx

from restaurant_ai.config import get_settings
from restaurant_ai.kernel.registry import display_name
from restaurant_ai.logging_setup import get_logger

log = get_logger(__name__)

CALLBACK_LIMIT = 64


@dataclass
class Interaction:
    approval_id: str
    approved: bool
    user: str
    note: str | None = None


def build_message(request: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """Render the approval as Telegram text plus an inline keyboard."""
    value = Decimal(str(request.get("value", "0")))
    detail = str(request.get("detail", ""))
    # Telegram caps a message at 4096 characters.
    if len(detail) > 3200:
        detail = detail[:3200] + "\n... (truncated)"

    text = (
        f"*Approval needed*\n"
        f"Agent: `{display_name(str(request.get('agent_name')))}`\n"
        f"Value: *{value:,.2f}*\n\n"
        f"{request.get('title')}\n\n"
        f"```\n{detail}\n```"
    )

    approval_id = request["id"]
    keyboard = {
        "inline_keyboard": [
            [
                {"text": "Approve", "callback_data": _callback("ok", approval_id)},
                {"text": "Reject", "callback_data": _callback("no", approval_id)},
            ]
        ]
    }
    return text, keyboard


def _callback(verdict: str, approval_id: str) -> str:
    data = f"{verdict}:{approval_id}"
    if len(data.encode()) > CALLBACK_LIMIT:
        # Should not happen with a UUID, but truncating silently would produce
        # a callback that resolves the wrong request.
        raise ValueError(
            f"Callback data is {len(data.encode())} bytes, over Telegram's {CALLBACK_LIMIT}."
        )
    return data


def send_approval_card(request: dict[str, Any]) -> str | None:
    settings = get_settings()
    if not settings.telegram_bot_token or not settings.telegram_chat_id:
        log.warning("telegram approval requested but bot token or chat id is not set")
        return None

    text, keyboard = build_message(request)
    try:
        response = httpx.post(
            f"https://api.telegram.org/bot{settings.telegram_bot_token}/sendMessage",
            json={
                "chat_id": settings.telegram_chat_id,
                "text": text,
                "parse_mode": "Markdown",
                "reply_markup": keyboard,
            },
            timeout=10,
        )
        body = response.json()
    except Exception as exc:
        log.error("telegram post failed", error=str(exc))
        return None

    if not body.get("ok"):
        log.error("telegram rejected the message", error=body.get("description"))
        return None
    return str(body.get("result", {}).get("message_id"))


def parse_callback(body: dict[str, Any]) -> Interaction | None:
    """Extract the decision from a Telegram callback_query update."""
    query = body.get("callback_query")
    if not query:
        return None

    data = query.get("data", "")
    verdict, _, approval_id = data.partition(":")
    if verdict not in ("ok", "no") or not approval_id:
        return None

    user = query.get("from", {})
    name = user.get("username") or user.get("first_name") or str(user.get("id", "telegram-user"))
    return Interaction(
        approval_id=approval_id,
        approved=verdict == "ok",
        user=name,
        note=f"Resolved from Telegram by {name}",
    )
