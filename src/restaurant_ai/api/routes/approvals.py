"""Approval endpoints.

Three ways in, one path through: the Slack interactivity callback, the Telegram
callback, and a plain REST endpoint for the CLI and tests. All of them land on
``approvals.service.resolve``, which is the only thing that resumes a parked
graph.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from pydantic import BaseModel, Field

from restaurant_ai.api.auth import require_api_key, verify_telegram_secret
from restaurant_ai.approvals.service import list_pending, resolve
from restaurant_ai.logging_setup import get_logger

log = get_logger(__name__)
router = APIRouter(prefix="/approvals", tags=["approvals"])


class ResolveRequest(BaseModel):
    approved: bool
    resolved_by: str = Field("api", description="Who decided.")
    note: str | None = None


@router.get("", dependencies=[Depends(require_api_key)])
async def pending() -> dict[str, Any]:
    items = list_pending()
    return {"count": len(items), "pending": items}


@router.post("/{approval_id}/resolve", dependencies=[Depends(require_api_key)])
async def resolve_approval(approval_id: str, request: ResolveRequest) -> dict[str, Any]:
    try:
        outcome = resolve(
            approval_id,
            approved=request.approved,
            resolved_by=request.resolved_by,
            note=request.note,
        )
    except KeyError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    return outcome


@router.post("/slack/interactivity")
async def slack_interactivity(request: Request) -> dict[str, Any]:
    """Slack Block Kit button callback.

    Slack posts form-encoded with a JSON `payload` field, and requires a
    response within three seconds, so this resolves and replies immediately
    rather than acknowledging and working in the background.
    """
    from restaurant_ai.approvals.slack import parse_interaction, verify_slack_request

    raw = await request.body()
    verify_slack_request(raw, dict(request.headers))

    interaction = parse_interaction(raw)
    if interaction is None:
        return {"text": "Nothing to do."}

    try:
        outcome = resolve(
            interaction.approval_id,
            approved=interaction.approved,
            resolved_by=interaction.user,
            note=interaction.note,
            channel="slack",
        )
    except (KeyError, ValueError) as exc:
        return {"replace_original": False, "text": f"Could not resolve: {exc}"}

    verb = "Approved" if interaction.approved else "Rejected"
    return {
        "replace_original": True,
        "text": f"{verb} by {interaction.user}. {outcome.get('summary', '')}",
    }


@router.post("/telegram/callback")
async def telegram_callback(
    request: Request,
    x_telegram_bot_api_secret_token: str = Header(default=""),
) -> dict[str, Any]:
    """Telegram inline-keyboard callback.

    The secret token is checked before the body is read. It is the only thing
    that distinguishes Telegram from anyone else who can reach this URL, and
    without it a forged callback_query approves whatever id it names.
    """
    from restaurant_ai.approvals.telegram import parse_callback

    verify_telegram_secret(x_telegram_bot_api_secret_token)

    body = await request.json()
    interaction = parse_callback(body)
    if interaction is None:
        return {"ok": True, "note": "Not an approval callback."}

    try:
        outcome = resolve(
            interaction.approval_id,
            approved=interaction.approved,
            resolved_by=interaction.user,
            note=interaction.note,
            channel="telegram",
        )
    except (KeyError, ValueError) as exc:
        return {"ok": False, "error": str(exc)}

    return {"ok": True, **outcome}
