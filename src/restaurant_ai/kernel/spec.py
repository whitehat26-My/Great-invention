"""How an agent is declared.

An agent is a prompt, a set of tools, a model and an approval policy. Nothing
else varies between the 13, so this is the whole surface area of adding one.

Approval policy lives on the tool rather than inside agent code, which means
"drafting a purchase order needs a human" is a property of the action itself and
cannot be forgotten by whoever writes the next agent that drafts one.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Literal

from sqlalchemy.orm import Session

ModelTier = Literal["reasoning", "conversational"]


@dataclass
class ToolContext:
    """What a tool is handed besides its own arguments.

    Tools receive a live session and the run's identity so their effects are
    traceable back to the run that caused them, and so everything a tool writes
    lands in the same transaction as the audit record.
    """

    session: Session
    run_id: str
    agent_name: str
    business_date: Any
    state: dict[str, Any] = field(default_factory=dict)


@dataclass
class ToolSpec:
    """One capability an agent has.

    ``requires_approval`` makes the tool return a Proposal instead of acting.
    ``approval_value`` extracts the monetary size of the action from its result,
    which is what the value-threshold policy is applied to and what a human sees
    in the approval card.
    """

    name: str
    description: str
    fn: Callable[..., dict[str, Any]]
    args_schema: type | None = None
    requires_approval: bool = False
    # Optional predicate on the result: when it returns False the invocation is
    # not gated, because there is nothing to approve. A reorder sweep that finds
    # everything in stock still "drafts purchase orders", it just drafts none —
    # and waking someone in Slack to approve an empty order is how people learn
    # to rubber-stamp the alerts that do matter.
    gate_when: Callable[[dict[str, Any]], bool] | None = None
    approval_value: Callable[[dict[str, Any]], Decimal] | None = None
    approval_summary: Callable[[dict[str, Any]], str] | None = None
    approval_detail: Callable[[dict[str, Any]], str] | None = None

    def should_gate(self, result: dict[str, Any]) -> bool:
        if not self.requires_approval:
            return False
        if self.gate_when is None:
            return True
        try:
            return bool(self.gate_when(result))
        except Exception:
            # A broken predicate must fail toward asking a human, never toward
            # acting unsupervised.
            return True

    def value_of(self, result: dict[str, Any]) -> Decimal:
        if self.approval_value is None:
            return Decimal("0")
        try:
            return Decimal(str(self.approval_value(result)))
        except Exception:
            return Decimal("0")

    def summarise(self, result: dict[str, Any]) -> str:
        if self.approval_summary is not None:
            try:
                return self.approval_summary(result)
            except Exception:
                pass
        return f"{self.name} requires approval"

    def detail_of(self, result: dict[str, Any]) -> str:
        if self.approval_detail is not None:
            try:
                return self.approval_detail(result)
            except Exception:
                pass
        return str(result)


@dataclass
class AgentSpec:
    """A complete agent definition."""

    name: str
    department: str
    title: str
    description: str
    system_prompt: str
    # What the agent is called. `name` is the slug everything keys off — the
    # CLI, the Celery schedule, every audit row — so it does not move. `person`
    # is what a human sees: an approval that reads "Rain is asking for MYR
    # 172.50 of flour" is easier to act on at 6am than one that reads
    # "stock_reorder". Defaulted so a throwaway spec in a test need not invent
    # one; the registry test is what holds the real thirteen to it.
    person: str = ""
    tools: list[ToolSpec] = field(default_factory=list)
    model_tier: ModelTier = "conversational"
    # Loads the agent's read-only view of the world. No LLM, no writes.
    perceive: Callable[[ToolContext], dict[str, Any]] | None = None
    # Optional deterministic path that runs instead of the LLM. Used by agents
    # whose work is purely computational, and by every agent under the fake
    # model so the platform is exercisable without an API key.
    autonomous: Callable[[ToolContext, dict[str, Any]], dict[str, Any]] | None = None
    max_iterations: int = 6

    def tool(self, name: str) -> ToolSpec | None:
        return next((t for t in self.tools if t.name == name), None)

    @property
    def gated_tools(self) -> list[ToolSpec]:
        return [t for t in self.tools if t.requires_approval]
