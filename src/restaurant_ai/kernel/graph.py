"""The shared agent graph.

    START -> perceive -> reason -> act -+- (nothing gated) ----------> record -> END
                                        |
                                        +- await_approval -> commit -> record -> END

perceive   loads the agent's read-only view of the world. No LLM, no writes.
reason     decides what to do: the LLM bound to this agent's tools, or the
           agent's deterministic autonomous path when the fake model is active.
act        runs the chosen tools. A gated tool returns a Proposal rather than
           acting, so nothing irreversible happens before a human sees it.
await_approval  interrupts. The graph checkpoints to Postgres and the process
           unwinds; it resumes later, possibly in another process entirely.
commit     performs the approved proposals inside one transaction.
record     writes the audit trail and publishes domain events.

Splitting act from commit is the important part. Preparing an action and
performing it are separate steps with a human in between, and the agent cannot
skip from one to the other.
"""

from __future__ import annotations

import json
import traceback
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage
from langgraph.graph import END, START, StateGraph

from restaurant_ai import clock
from restaurant_ai.db.base import session_scope
from restaurant_ai.db.models import AgentRunStatus
from restaurant_ai.kernel import audit, llm
from restaurant_ai.kernel.approval import (
    apply_decision,
    build_proposal,
    needs_approval,
    request_approval,
)
from restaurant_ai.kernel.spec import AgentSpec, ToolContext
from restaurant_ai.kernel.state import ActionRecord, AgentState
from restaurant_ai.logging_setup import get_logger

log = get_logger(__name__)


def build_graph(spec: AgentSpec):
    """Compile the graph for one agent. Identical shape for all 13."""

    def perceive(state: AgentState) -> dict[str, Any]:
        if spec.perceive is None:
            return {"context": {}}
        with session_scope() as session:
            context = spec.perceive(
                ToolContext(
                    session=session,
                    run_id=state["run_id"],
                    agent_name=spec.name,
                    business_date=state["business_date"],
                    state=dict(state.get("trigger_payload") or {}),
                )
            )
        return {"context": context or {}}

    def reason(state: AgentState) -> dict[str, Any]:
        """Choose the actions to take.

        Under the fake model the agent's autonomous path runs instead of the
        LLM, so offline behaviour is the real deterministic logic rather than a
        scripted imitation of reasoning.
        """
        if llm.is_fake() or spec.autonomous is not None:
            if spec.autonomous is None:
                return {
                    "results": {},
                    "summary": f"{spec.title}: no autonomous path defined and no model available.",
                }
            with session_scope() as session:
                context = ToolContext(
                    session=session,
                    run_id=state["run_id"],
                    agent_name=spec.name,
                    business_date=state["business_date"],
                    state=dict(state.get("trigger_payload") or {}),
                )
                outcome = spec.autonomous(context, state.get("context") or {})
            calls = outcome.get("tool_calls") or []
            return {
                "results": outcome.get("results", {}),
                "summary": outcome.get("summary", ""),
                "context": {**(state.get("context") or {}), "_planned_calls": calls},
            }

        return _reason_with_model(spec, state)

    def act(state: AgentState) -> dict[str, Any]:
        """Execute the planned tool calls, gating the ones that need a human."""
        planned = (state.get("context") or {}).get("_planned_calls") or []
        if not planned:
            return {"proposals": [], "actions": []}

        proposals = list(state.get("proposals") or [])
        actions = list(state.get("actions") or [])
        results = dict(state.get("results") or {})

        with session_scope() as session:
            context = ToolContext(
                session=session,
                run_id=state["run_id"],
                agent_name=spec.name,
                business_date=state["business_date"],
                state=dict(state.get("trigger_payload") or {}),
            )
            for call in planned:
                name = call.get("name")
                arguments = call.get("args") or {}
                tool = spec.tool(name)
                if tool is None:
                    actions.append(
                        ActionRecord(
                            tool_name=str(name),
                            arguments=arguments,
                            result={},
                            error=f"Unknown tool {name!r}",
                            occurred_at=clock.utcnow(),
                        )
                    )
                    continue

                try:
                    result = tool.fn(context, **arguments) or {}
                except Exception as exc:  # a failing tool must not kill the run
                    log.warning("tool failed", agent=spec.name, tool=name, error=str(exc))
                    actions.append(
                        ActionRecord(
                            tool_name=name,
                            arguments=arguments,
                            result={},
                            error=f"{type(exc).__name__}: {exc}",
                            occurred_at=clock.utcnow(),
                        )
                    )
                    continue

                gated = needs_approval(tool, result)
                actions.append(
                    ActionRecord(
                        tool_name=name,
                        arguments=arguments,
                        result=result,
                        is_proposal=gated,
                        occurred_at=clock.utcnow(),
                    )
                )
                if gated:
                    proposals.append(build_proposal(tool, result))
                else:
                    results[name] = result

        return {"proposals": proposals, "actions": actions, "results": results}

    def await_approval(state: AgentState) -> dict[str, Any]:
        """Stop for a human.

        `interrupt` does not return here. The graph checkpoints and unwinds; the
        value below is produced only when someone later resumes the thread.
        """
        pending = [p for p in state.get("proposals") or [] if p.is_pending]
        if not pending:
            return {}

        # Persist each proposal as a resolvable request BEFORE interrupting.
        # Once interrupt() unwinds the graph, this node's remaining code does
        # not run until someone resumes it — so a request written afterwards
        # would never exist, and there would be nothing for Slack to resolve.
        from restaurant_ai.approvals.service import record_request

        with session_scope() as session:
            audit.mark_awaiting_approval(session, state["run_id"])
            for proposal in pending:
                record_request(
                    run_id=state["run_id"],
                    thread_id=state["thread_id"],
                    agent_name=spec.name,
                    title=proposal.summary,
                    detail=proposal.detail,
                    payload=audit._jsonable(proposal.payload),
                    value=proposal.value,
                    session=session,
                )

        decision = request_approval(pending, spec.name, state["run_id"])
        return {"proposals": apply_decision(pending, decision)}

    def commit(state: AgentState) -> dict[str, Any]:
        """Carry out the approved proposals, all inside one transaction."""
        proposals = state.get("proposals") or []
        approved = [p for p in proposals if p.approved]
        rejected = [p for p in proposals if p.approved is False]

        actions = list(state.get("actions") or [])
        results = dict(state.get("results") or {})
        committed: list[str] = []

        if approved:
            with session_scope() as session:
                context = ToolContext(
                    session=session,
                    run_id=state["run_id"],
                    agent_name=spec.name,
                    business_date=state["business_date"],
                    state=dict(state.get("trigger_payload") or {}),
                )
                for proposal in approved:
                    tool = spec.tool(proposal.tool_name)
                    committer = getattr(tool, "commit_fn", None) if tool else None
                    # The approver's identity lives on the proposal, not in the
                    # tool's result. Merge it in so the committer can record who
                    # authorised the spend — the field that makes an approval
                    # trail worth keeping.
                    payload = {
                        **proposal.payload,
                        "approved_by": proposal.resolved_by,
                        "approval_note": proposal.resolution_note,
                    }
                    try:
                        outcome = (
                            committer(context, payload)
                            if committer
                            else _default_commit(context, payload)
                        )
                        results[proposal.tool_name] = outcome
                        committed.append(proposal.tool_name)
                        actions.append(
                            ActionRecord(
                                tool_name=f"{proposal.tool_name}:commit",
                                arguments={"approved_by": proposal.resolved_by},
                                result=outcome,
                                occurred_at=clock.utcnow(),
                            )
                        )
                    except Exception as exc:
                        log.error(
                            "commit failed",
                            agent=spec.name,
                            tool=proposal.tool_name,
                            error=str(exc),
                        )
                        actions.append(
                            ActionRecord(
                                tool_name=f"{proposal.tool_name}:commit",
                                arguments={},
                                result={},
                                error=f"{type(exc).__name__}: {exc}",
                                occurred_at=clock.utcnow(),
                            )
                        )

        return {
            "actions": actions,
            "results": results,
            "context": {
                **(state.get("context") or {}),
                "_committed": committed,
                "_rejected": [p.tool_name for p in rejected],
            },
        }

    def record(state: AgentState) -> dict[str, Any]:
        """Persist the audit trail and settle the run's final status."""
        proposals = state.get("proposals") or []
        rejected = [p for p in proposals if p.approved is False]
        approved = [p for p in proposals if p.approved]

        summary = state.get("summary") or ""
        if approved or rejected:
            parts = [summary] if summary else []
            if approved:
                parts.append(f"{len(approved)} action(s) approved and committed.")
            if rejected:
                parts.append(f"{len(rejected)} action(s) rejected.")
            summary = " ".join(parts)

        # A tool that raised is recorded and the run continues, which is right —
        # one broken tool should not lose the rest of the work. But the run must
        # not then report clean success: that is how a crash in the pacing agent
        # went unnoticed while it silently stopped sending tickets to the pass.
        failed_actions = [a for a in (state.get("actions") or []) if a.error]
        if failed_actions:
            names = ", ".join(sorted({a.tool_name for a in failed_actions}))
            summary = (summary + " " if summary else "") + (
                f"{len(failed_actions)} tool call(s) failed ({names}); that work was not done."
            )

        if state.get("error"):
            status = AgentRunStatus.FAILED
        elif failed_actions and not state.get("results"):
            # Everything the agent tried to do failed.
            status = AgentRunStatus.FAILED
        elif rejected and not approved:
            status = AgentRunStatus.REJECTED
        else:
            status = AgentRunStatus.COMPLETED

        with session_scope() as session:
            audit.record_actions(session, state["run_id"], state.get("actions") or [])
            audit.finish_run(
                session,
                state["run_id"],
                status=status,
                summary=summary,
                error=state.get("error"),
                results=state.get("results") or {},
            )

        return {"summary": summary}

    def route_after_act(state: AgentState) -> str:
        pending = [p for p in state.get("proposals") or [] if p.is_pending]
        return "await_approval" if pending else "record"

    builder = StateGraph(AgentState)
    builder.add_node("perceive", perceive)
    builder.add_node("reason", reason)
    builder.add_node("act", act)
    builder.add_node("await_approval", await_approval)
    builder.add_node("commit", commit)
    builder.add_node("record", record)

    builder.add_edge(START, "perceive")
    builder.add_edge("perceive", "reason")
    builder.add_edge("reason", "act")
    builder.add_conditional_edges(
        "act", route_after_act, {"await_approval": "await_approval", "record": "record"}
    )
    builder.add_edge("await_approval", "commit")
    builder.add_edge("commit", "record")
    builder.add_edge("record", END)

    return builder


def _default_commit(context: ToolContext, payload: dict[str, Any]) -> dict[str, Any]:
    """Fallback when a gated tool declares no committer.

    Most gated tools write their effect as a draft during ``act`` (a purchase
    order in DRAFT, a price change proposal) and only need a status transition
    on approval, which their own commit function performs. Reaching here means
    the tool did not say what approval should do, so it is recorded rather than
    guessed at.
    """
    return {"committed": True, "payload": payload, "note": "No commit handler; recorded only."}


def _reason_with_model(spec: AgentSpec, state: AgentState) -> dict[str, Any]:
    """Live-model reasoning: bind the agent's tools and let it choose."""
    model = llm.get_model(spec.model_tier)
    tools = [_as_langchain_tool(t) for t in spec.tools]
    bound = model.bind_tools(tools) if tools else model

    messages: list[Any] = [
        SystemMessage(content=spec.system_prompt),
        HumanMessage(content=_context_prompt(state)),
    ]

    response = bound.invoke(messages)
    calls = [
        {"name": call["name"], "args": call.get("args") or {}}
        for call in (getattr(response, "tool_calls", None) or [])
    ]
    text = response.content if isinstance(response.content, str) else str(response.content)

    return {
        "messages": [response],
        "summary": text,
        "context": {**(state.get("context") or {}), "_planned_calls": calls},
    }


def _context_prompt(state: AgentState) -> str:
    """Render what perceive loaded into the message the model actually sees."""
    context = {k: v for k, v in (state.get("context") or {}).items() if not k.startswith("_")}
    return (
        f"Business date: {state['business_date']}\n"
        f"Trigger: {state.get('trigger')} ({state.get('trigger_ref') or 'n/a'})\n\n"
        f"Current situation:\n{json.dumps(audit._jsonable(context), indent=2, default=str)}\n\n"
        f"Decide what to do and call the tools you need. If nothing needs doing, say so."
    )


def _as_langchain_tool(spec: Any):
    """Expose a ToolSpec to the model.

    Only the schema is handed over: the model chooses a tool and its arguments,
    and the graph's ``act`` node is what actually calls it, with the session and
    run identity the model never sees.
    """
    from langchain_core.tools import StructuredTool

    def _stub(**kwargs: Any) -> str:  # pragma: no cover - never invoked
        return "Tool execution is handled by the agent runtime."

    return StructuredTool.from_function(
        func=_stub,
        name=spec.name,
        description=spec.description,
        args_schema=spec.args_schema,
    )


def format_exception(exc: BaseException) -> str:
    return "".join(traceback.format_exception_only(type(exc), exc)).strip()
