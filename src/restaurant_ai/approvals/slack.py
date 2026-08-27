"""Slack approval cards.

A Block Kit message with Approve and Reject buttons. The approval id rides in
the button value, so the interactivity callback knows which parked graph to
resume without any server-side session.

Slack's own signature scheme is verified here — the interactivity endpoint is
public, and without verification anyone who found the URL could approve
purchase orders.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
import urllib.parse
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import httpx

from restaurant_ai.config import get_settings
from restaurant_ai.kernel.registry import display_name
from restaurant_ai.logging_setup import get_logger

log = get_logger(__name__)

SLACK_API = "https://slack.com/api/chat.postMessage"
# Slack signs with a versioned prefix; v0 is current.
SIGNATURE_VERSION = "v0"
MAX_SKEW_SECONDS = 300


@dataclass
class Interaction:
    approval_id: str
    approved: bool
    user: str
    note: str | None = None


def build_blocks(request: dict[str, Any]) -> list[dict[str, Any]]:
    """Render the approval as Block Kit.

    The detail is truncated to Slack's 3000-character section limit; the full
    proposal stays in the database and the CLI can show it.
    """
    value = Decimal(str(request.get("value", "0")))
    detail = str(request.get("detail", ""))
    if len(detail) > 2800:
        detail = detail[:2800] + "\n... (truncated; see the full proposal in the run record)"

    blocks: list[dict[str, Any]] = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": "Approval needed", "emoji": False},
        },
        {
            "type": "section",
            "fields": [
                {
                    "type": "mrkdwn",
                    "text": f"*Agent*\n{display_name(str(request.get('agent_name')))}",
                },
                {"type": "mrkdwn", "text": f"*Value*\n{value:,.2f}"},
            ],
        },
        {"type": "section", "text": {"type": "mrkdwn", "text": f"*{request.get('title')}*"}},
        {"type": "section", "text": {"type": "mrkdwn", "text": f"```{detail}```"}},
        {
            "type": "actions",
            "block_id": f"approval:{request['id']}",
            "elements": [
                {
                    "type": "button",
                    "style": "primary",
                    "text": {"type": "plain_text", "text": "Approve"},
                    "action_id": "approve",
                    "value": request["id"],
                    # Spending money should take one deliberate extra click.
                    "confirm": {
                        "title": {"type": "plain_text", "text": "Confirm approval"},
                        "text": {
                            "type": "mrkdwn",
                            "text": f"This will commit *{value:,.2f}* of activity immediately.",
                        },
                        "confirm": {"type": "plain_text", "text": "Approve"},
                        "deny": {"type": "plain_text", "text": "Cancel"},
                    },
                },
                {
                    "type": "button",
                    "style": "danger",
                    "text": {"type": "plain_text", "text": "Reject"},
                    "action_id": "reject",
                    "value": request["id"],
                },
            ],
        },
    ]
    return blocks


def send_approval_card(request: dict[str, Any]) -> str | None:
    """Post the card to Slack. Returns the message ref, or None if unconfigured."""
    settings = get_settings()
    if not settings.slack_bot_token:
        log.warning("slack approval requested but SLACK_BOT_TOKEN is not set")
        return None

    payload = {
        "channel": settings.slack_approval_channel,
        "text": f"Approval needed: {request.get('title')}",  # notification fallback
        "blocks": build_blocks(request),
    }
    try:
        response = httpx.post(
            SLACK_API,
            json=payload,
            headers={"Authorization": f"Bearer {settings.slack_bot_token}"},
            timeout=10,
        )
        body = response.json()
    except Exception as exc:
        log.error("slack post failed", error=str(exc))
        return None

    if not body.get("ok"):
        log.error("slack rejected the message", error=body.get("error"))
        return None
    return f"{body.get('channel')}:{body.get('ts')}"


def verify_slack_request(body: bytes, headers: dict[str, str]) -> None:
    """Verify Slack's request signature.

    Skipped when no signing secret is configured, which is the local-development
    case; in any deployment where Slack is actually wired up the secret exists
    and this is enforced.
    """
    settings = get_settings()
    if not settings.slack_signing_secret:
        return

    from fastapi import HTTPException, status

    timestamp = headers.get("x-slack-request-timestamp", "")
    signature = headers.get("x-slack-signature", "")
    if not timestamp or not signature:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing Slack signature headers."
        )

    try:
        skew = abs(int(time.time()) - int(timestamp))
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Malformed Slack timestamp."
        ) from None

    if skew > MAX_SKEW_SECONDS:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Slack request is too old."
        )

    basestring = f"{SIGNATURE_VERSION}:{timestamp}:".encode() + body
    expected = (
        f"{SIGNATURE_VERSION}="
        + hmac.new(settings.slack_signing_secret.encode(), basestring, hashlib.sha256).hexdigest()
    )
    if not hmac.compare_digest(expected, signature):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Slack signature mismatch."
        )


def parse_interaction(body: bytes) -> Interaction | None:
    """Extract the decision from Slack's form-encoded interactivity payload."""
    try:
        form = urllib.parse.parse_qs(body.decode())
        raw = form.get("payload", ["{}"])[0]
        payload = json.loads(raw)
    except Exception as exc:
        log.warning("could not parse slack interaction", error=str(exc))
        return None

    actions = payload.get("actions") or []
    if not actions:
        return None

    action = actions[0]
    action_id = action.get("action_id")
    if action_id not in ("approve", "reject"):
        return None

    user = payload.get("user", {})
    return Interaction(
        approval_id=action.get("value", ""),
        approved=action_id == "approve",
        user=user.get("username") or user.get("name") or user.get("id") or "slack-user",
        note=f"Resolved from Slack by {user.get('username') or user.get('id')}",
    )
