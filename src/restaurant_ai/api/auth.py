"""Who is allowed to resolve an approval.

The approval gate is the platform's whole safety story: five agents can spend
money or publish, and every one of them stops for a human. That guarantee is
worth exactly as much as the endpoint that records the human's answer.

It was worth nothing. `GET /approvals` listed every pending request with its id,
and `POST /approvals/telegram/callback` resolved any id it was handed, both
unauthenticated. Anything that could reach the port could approve a purchase
order and sign someone else's name to it.

So these fail closed. A missing secret means the endpoint refuses to serve, not
that it serves anyone — the alternative is a deployment that is wide open
because of a line nobody remembered to add to .env.
"""

from __future__ import annotations

import hmac

from fastapi import Header, HTTPException, status

from restaurant_ai.config import get_settings


def _unconfigured(setting: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail=(
            f"{setting} is not configured, so this endpoint is closed. Set it "
            f"before exposing this service."
        ),
    )


def require_api_key(x_api_key: str = Header(default="")) -> None:
    """Guard the endpoints that can resolve an approval or start an agent."""
    configured = get_settings().approval_api_key
    if not configured:
        raise _unconfigured("APPROVAL_API_KEY")
    if not hmac.compare_digest(x_api_key, configured):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Bad or missing X-API-Key."
        )


def verify_telegram_secret(header_value: str) -> None:
    """Check the secret Telegram echoes back on every webhook call.

    Set via `secret_token` on setWebhook, which is the only thing distinguishing
    a real Telegram callback from anyone else's POST.
    """
    configured = get_settings().telegram_webhook_secret
    if not configured:
        raise _unconfigured("TELEGRAM_WEBHOOK_SECRET")
    if not hmac.compare_digest(header_value or "", configured):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Bad or missing X-Telegram-Bot-Api-Secret-Token.",
        )
