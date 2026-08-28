"""Agent control: list them, run one, inspect what a run did.

Every route here is behind the API key, reads included. Running an agent
obviously needs authority; *reading* the runs needed it too and did not have it
— an agent run's summary is the restaurant's business ("drafting purchase
orders for anything at or below the updated reorder points", with values), and
the run list is a diary of what the restaurant did and when. That was harmless
while this only ever answered on localhost, and stopped being harmless the
moment a tunnel gave it a public address.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select

from restaurant_ai.api.auth import require_api_key
from restaurant_ai.db.base import session_scope
from restaurant_ai.db.models import AgentAction, AgentRun
from restaurant_ai.kernel.registry import all_agents, get_agent
from restaurant_ai.kernel.runner import run_agent

# Applied to the router rather than route by route: a new endpoint here should
# be closed because it is here, not because somebody remembered.
router = APIRouter(prefix="/agents", tags=["agents"], dependencies=[Depends(require_api_key)])


class RunRequest(BaseModel):
    business_date: str | None = Field(None, description="ISO date; defaults to today.")
    trigger: str = Field("api", description="What caused this run.")
    trigger_ref: str | None = None


@router.get("")
async def list_agents() -> dict[str, Any]:
    agents = all_agents()
    return {
        "count": len(agents),
        "agents": [
            {
                "name": spec.name,
                "department": spec.department,
                "title": spec.title,
                "description": spec.description,
                "model_tier": spec.model_tier,
                "tools": [t.name for t in spec.tools],
                "requires_approval_for": [t.name for t in spec.gated_tools],
            }
            for spec in sorted(agents.values(), key=lambda s: (s.department, s.name))
        ],
    }


@router.post("/{name}/run", status_code=status.HTTP_200_OK)
async def trigger_run(name: str, request: RunRequest) -> dict[str, Any]:
    """Run an agent now.

    Returns 202 semantics in the body rather than the status code when the agent
    parks for approval, because the run genuinely is unfinished and the caller
    needs the thread id to resume it.
    """
    try:
        spec = get_agent(name)
    except KeyError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc

    outcome = run_agent(
        spec,
        business_date=date.fromisoformat(request.business_date) if request.business_date else None,
        trigger=request.trigger,
        trigger_ref=request.trigger_ref,
    )
    return {
        "agent": name,
        "run_id": outcome.run_id,
        "thread_id": outcome.thread_id,
        "status": "awaiting_approval" if outcome.interrupted else "completed",
        "summary": outcome.summary,
        "error": outcome.error,
        "approval_request": outcome.interrupt_payload,
        "results": _summarise(outcome.results),
    }


@router.get("/runs")
async def list_runs(limit: int = 25, agent: str | None = None) -> dict[str, Any]:
    with session_scope() as session:
        stmt = select(AgentRun).order_by(AgentRun.started_at.desc()).limit(limit)
        if agent:
            stmt = stmt.where(AgentRun.agent_name == agent)
        runs = list(session.execute(stmt).scalars())
        return {
            "count": len(runs),
            "runs": [
                {
                    "run_id": r.id,
                    "agent": r.agent_name,
                    "department": r.department,
                    "status": r.status.value,
                    "trigger": r.trigger,
                    "business_date": r.business_date.isoformat(),
                    "started_at": r.started_at.isoformat(),
                    "summary": r.summary,
                }
                for r in runs
            ],
        }


@router.get("/runs/{run_id}")
async def get_run(run_id: str) -> dict[str, Any]:
    """One run in full, including every tool call it made."""
    with session_scope() as session:
        run = session.get(AgentRun, run_id)
        if run is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"No run {run_id}.")
        actions = list(
            session.execute(
                select(AgentAction)
                .where(AgentAction.run_id == run_id)
                .order_by(AgentAction.sequence)
            ).scalars()
        )
        return {
            "run_id": run.id,
            "agent": run.agent_name,
            "department": run.department,
            "status": run.status.value,
            "trigger": run.trigger,
            "business_date": run.business_date.isoformat(),
            "started_at": run.started_at.isoformat(),
            "finished_at": run.finished_at.isoformat() if run.finished_at else None,
            "model": run.model,
            "summary": run.summary,
            "error": run.error,
            "actions": [
                {
                    "sequence": a.sequence,
                    "tool": a.tool_name,
                    "arguments": a.arguments,
                    "is_proposal": a.is_proposal,
                    "committed": a.committed_at is not None,
                    "error": a.error,
                    "result": a.result,
                }
                for a in actions
            ],
        }


def _summarise(results: dict[str, Any]) -> dict[str, Any]:
    """Trim tool results so a run response stays readable."""
    trimmed: dict[str, Any] = {}
    for tool, value in results.items():
        if isinstance(value, dict):
            trimmed[tool] = {
                k: v
                for k, v in value.items()
                if not isinstance(v, (list, dict)) or len(str(v)) < 400
            }
        else:
            trimmed[tool] = value
    return trimmed
