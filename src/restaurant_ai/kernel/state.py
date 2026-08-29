"""The state every agent graph carries.

LangGraph merges node returns into this dict, so anything a later node needs
must live here. Two fields do the real work:

``proposals`` holds actions a gated tool declined to perform, each waiting on a
human. ``results`` holds what actually happened, and is what the audit trail and
the agent's own summary are built from.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Annotated, Any, TypedDict

from langchain_core.messages import BaseMessage
from langgraph.graph.message import add_messages


@dataclass
class Proposal:
    """An action a gated tool prepared but did not perform.

    The payload is everything ``commit`` needs to carry the action out once a
    human says yes, and ``summary``/``detail`` are what that human actually
    reads in Slack, so they have to stand on their own.
    """

    tool_name: str
    summary: str
    detail: str
    payload: dict[str, Any]
    value: Decimal = Decimal("0")
    approved: bool | None = None
    resolved_by: str | None = None
    resolution_note: str | None = None

    @property
    def is_pending(self) -> bool:
        return self.approved is None

    def to_payload(self) -> dict[str, Any]:
        return {
            "tool_name": self.tool_name,
            "summary": self.summary,
            "detail": self.detail,
            "payload": self.payload,
            "value": str(self.value),
        }


@dataclass
class ActionRecord:
    """One tool call, for the audit trail."""

    tool_name: str
    arguments: dict[str, Any]
    result: dict[str, Any]
    is_proposal: bool = False
    error: str | None = None
    occurred_at: datetime | None = None


class AgentState(TypedDict, total=False):
    """Shared across every agent."""

    # Identity
    agent_name: str
    department: str
    run_id: str
    thread_id: str
    business_date: date

    # Why this run happened
    trigger: str
    trigger_ref: str | None
    trigger_payload: dict[str, Any]

    # What `perceive` loaded: the agent's read-only view of the world
    context: dict[str, Any]

    # The reasoning transcript
    messages: Annotated[list[BaseMessage], add_messages]

    # Gated actions awaiting a human, and everything that was actually done
    proposals: list[Proposal]
    actions: list[ActionRecord]
    results: dict[str, Any]

    # Outcome
    summary: str
    error: str | None
    iterations: int


def initial_state(
    agent_name: str,
    department: str,
    run_id: str,
    thread_id: str,
    business_date: date,
    trigger: str = "manual",
    trigger_ref: str | None = None,
    trigger_payload: dict[str, Any] | None = None,
) -> AgentState:
    return AgentState(
        agent_name=agent_name,
        department=department,
        run_id=run_id,
        thread_id=thread_id,
        business_date=business_date,
        trigger=trigger,
        trigger_ref=trigger_ref,
        trigger_payload=trigger_payload or {},
        context={},
        messages=[],
        proposals=[],
        actions=[],
        results={},
        summary="",
        error=None,
        iterations=0,
    )
