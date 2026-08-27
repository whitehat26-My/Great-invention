"""The approval gate.

All LangGraph human-in-the-loop machinery is confined to this module and
graph.py, so the rest of the platform never imports it directly.

The mechanism: a node calls ``interrupt(payload)``. LangGraph checkpoints the
graph to Postgres and unwinds, so the worker process is free — an approval can
sit for hours without holding anything open. Later, and in a different process
(the Slack webhook handler), ``Command(resume=decision)`` picks the graph back
up mid-node and it runs on to commit.

That restartability is the whole reason for the checkpointer. A purchase order
awaiting sign-off must survive a deploy.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from langgraph.types import interrupt

from restaurant_ai.config import get_settings
from restaurant_ai.kernel.spec import ToolSpec
from restaurant_ai.kernel.state import Proposal


def needs_approval(tool: ToolSpec, result: dict[str, Any]) -> bool:
    """Whether this particular invocation has to stop for a human.

    A tool declared ``requires_approval`` always does. Anything else is gated
    only once its value crosses the configured threshold, so routine small
    actions stay autonomous and only consequential ones interrupt someone.
    """
    if tool.requires_approval:
        return True
    threshold = get_settings().approval_value_threshold
    return threshold > 0 and tool.value_of(result) >= threshold


def build_proposal(tool: ToolSpec, result: dict[str, Any]) -> Proposal:
    return Proposal(
        tool_name=tool.name,
        summary=tool.summarise(result),
        detail=tool.detail_of(result),
        payload=result,
        value=tool.value_of(result),
    )


def request_approval(proposals: list[Proposal], agent_name: str, run_id: str) -> dict[str, Any]:
    """Pause the graph until a human decides.

    Returns the resume value: a mapping of tool name to decision. Execution does
    not continue past this call until someone answers.
    """
    payload = {
        "kind": "approval_request",
        "agent": agent_name,
        "run_id": run_id,
        "proposals": [p.to_payload() for p in proposals],
        "total_value": str(sum((p.value for p in proposals), Decimal("0"))),
    }
    return interrupt(payload)


def apply_decision(proposals: list[Proposal], decision: Any) -> list[Proposal]:
    """Fold a human's answer back onto the proposals.

    Accepts several shapes, because the answer arrives from a Slack button, a
    Telegram callback, the CLI or a test:

        True / "approve"                     -> approve everything
        False / "reject"                     -> reject everything
        {"approved": bool, "by": str, ...}   -> one decision for all
        {"<tool_name>": bool, ...}           -> per-proposal decisions

    Anything unrecognised is treated as a rejection. Defaulting an ambiguous
    answer to "yes, spend the money" would be the wrong way round.
    """
    if decision is None:
        return _resolve_all(proposals, approved=False, by=None, note="No decision received")

    if isinstance(decision, bool):
        return _resolve_all(proposals, approved=decision, by=None)

    if isinstance(decision, str):
        normalised = decision.strip().lower()
        if normalised in ("approve", "approved", "yes", "y", "ok"):
            return _resolve_all(proposals, approved=True, by=None)
        return _resolve_all(proposals, approved=False, by=None, note=f"Rejected: {decision}")

    if isinstance(decision, dict):
        if "approved" in decision:
            return _resolve_all(
                proposals,
                approved=bool(decision["approved"]),
                by=decision.get("by") or decision.get("resolved_by"),
                note=decision.get("note") or decision.get("resolution_note"),
            )
        # Per-proposal mapping; anything unmentioned stays rejected.
        by = decision.get("by")
        for proposal in proposals:
            if proposal.tool_name in decision:
                proposal.approved = bool(decision[proposal.tool_name])
            else:
                proposal.approved = False
                proposal.resolution_note = "No decision recorded for this action"
            proposal.resolved_by = by
        return proposals

    return _resolve_all(
        proposals, approved=False, by=None, note=f"Unrecognised decision: {decision!r}"
    )


def _resolve_all(
    proposals: list[Proposal], approved: bool, by: str | None, note: str | None = None
) -> list[Proposal]:
    for proposal in proposals:
        proposal.approved = approved
        proposal.resolved_by = by
        if note:
            proposal.resolution_note = note
    return proposals
