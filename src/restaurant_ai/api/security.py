"""Webhook signature verification.

A webhook endpoint is an unauthenticated door into the platform: anything that
can POST to /webhooks/pos can create sales, move stock and ultimately trigger
purchase orders. So every payload carries an HMAC over its raw bytes plus a
timestamp, and both are checked before the body is even parsed.

The timestamp is what stops a replay: a valid signature captured off the wire is
otherwise valid forever.
"""

from __future__ import annotations

import hashlib
import hmac
import time

from fastapi import Header, HTTPException, Request, status

from restaurant_ai.config import get_settings


def sign_payload(body: bytes, timestamp: str, secret: str | None = None) -> str:
    """Produce the signature a sender must send. Also used by the simulator."""
    secret = secret or get_settings().webhook_secret
    message = timestamp.encode() + b"." + body
    return hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()


def verify_signature(body: bytes, timestamp: str, signature: str) -> None:
    """Reject anything unsigned, mis-signed or stale.

    Raises HTTPException; returns nothing on success.
    """
    settings = get_settings()

    if not signature or not timestamp:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing X-Signature or X-Timestamp header.",
        )

    try:
        sent_at = int(timestamp)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Malformed X-Timestamp."
        ) from None

    drift = abs(int(time.time()) - sent_at)
    if drift > settings.webhook_tolerance_seconds:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=(
                f"Timestamp is {drift}s out of tolerance "
                f"({settings.webhook_tolerance_seconds}s). Rejecting as a possible replay."
            ),
        )

    expected = sign_payload(body, timestamp)
    # Constant-time comparison: a fast-failing comparison leaks the signature
    # one byte at a time to anyone willing to measure.
    if not hmac.compare_digest(expected, signature):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Signature does not match."
        )


async def require_signature(
    request: Request,
    x_signature: str = Header(default=""),
    x_timestamp: str = Header(default=""),
) -> bytes:
    """FastAPI dependency: verify the signature and hand back the raw body."""
    body = await request.body()
    verify_signature(body, x_timestamp, x_signature)
    return body
