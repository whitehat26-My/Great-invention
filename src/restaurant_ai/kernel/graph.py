"""The shared agent graph.

                    +<-------- (more to do) --------+
                    |                               |
    START -> perceive -> reason -> act -+- (nothing gated) --> record -> END
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

The loop back from act to reason exists only on the live-model path, and it is
what makes the difference between an agent and a one-shot planner. A model that
never sees its tool results cannot look up a table and then book it, and its
closing summary describes what it *meant* to do rather than what happened. The
deterministic path does not loop: it decides its whole plan up front and already
knows the outcome.

The loop stops at the approval gate. When a tool proposes something gated the
run parks for a human, and no number of remaining iterations lets the model
carry on past it.
"""

from __future__ import annotations

import json
import traceback
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage
from langgraph.graph import END, START, StateGraph

from restaurant_ai import clock
from restaurant_ai.config import get_settings
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

        The live model plans when one is configured; otherwise the agent's
        deterministic path runs, so offline behaviour is the real logic rather
        than a scripted imitation of reasoning.

        The condition used to be ``is_fake() or spec.autonomous is not None``,
        and since all 13 agents define an autonomous path that meant the model
        was never asked anything — setting a real API key changed nothing at
        all. Which path runs is now a property of the configured provider, as
        it reads.
        """
        if _use_model(spec, state):
            return _reason_with_model(spec, state)

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
            "iterations": state.get("iterations", 0) + 1,
            "context": {
                **(state.get("context") or {}),
                "_planned_calls": calls,
                "_path": "autonomous",
            },
        }

    def act(state: AgentState) -> dict[str, Any]:
        """Execute the planned tool calls, gating the ones that need a human."""
        context_in = state.get("context") or {}
        planned = context_in.get("_planned_calls") or []
        if not planned:
            # Only `context` is returned. `proposals` and `actions` carry no
            # reducer, so naming them here would replace what earlier turns
            # accumulated with an empty list — and a run whose first turn failed
            # and whose last turn was quiet would reach `record` with a clean
            # slate and be filed as a success.
            return {"context": {**context_in, "_planned_calls": [], "_acted": 0}}

        proposals = list(state.get("proposals") or [])
        actions = list(state.get("actions") or [])
        results = dict(state.get("results") or {})
        # One tool_result per tool_use, or the next request is rejected outright.
        # These are also the whole point of the loop: what the model reads to
        # find out whether what it asked for actually worked.
        tool_messages: list[ToolMessage] = []

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
                    _reply(tool_messages, call, f"Error: no tool named {name!r} exists.")
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
                    # The model is told it failed and why, so it can correct the
                    # arguments or work around it rather than carrying on as if
                    # the call had succeeded.
                    _reply(tool_messages, call, f"Error: {type(exc).__name__}: {exc}")
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
                    proposal = build_proposal(tool, result)
                    proposals.append(proposal)
                    # Answered so the checkpointed transcript stays well formed
                    # — an unanswered tool_use in it is a 400 for whoever
                    # resumes. Worded as prepared rather than done, because the
                    # action has not happened and the transcript should not say
                    # it has.
                    _reply(
                        tool_messages,
                        call,
                        "Prepared, awaiting human approval — not yet performed. "
                        f"{proposal.summary}",
                    )
                else:
                    results[name] = result
                    _reply(tool_messages, call, _render_result(result))

        return {
            "proposals": proposals,
            "actions": actions,
            "results": results,
            "messages": tool_messages,
            # Cleared, or the next pass round the loop would run this same plan
            # again — every time, forever, up to the iteration cap.
            "context": {**context_in, "_planned_calls": [], "_acted": len(planned)},
        }

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
        context = state.get("context") or {}
        if (
            context.get("_path") == "model"
            and context.get("_acted")
            and state.get("iterations", 0) >= spec.max_iterations
        ):
            # It still wanted to do more when it ran out of turns, so the summary
            # above is the last thing it said and not a report on a finished job.
            summary = (summary + " " if summary else "") + (
                f"Stopped at the {spec.max_iterations}-turn reasoning limit with "
                "work still outstanding."
            )

        failed_actions = [a for a in (state.get("actions") or []) if a.error]
        if failed_actions:
            names = ", ".join(sorted({a.tool_name for a in failed_actions}))
            note = f"{len(failed_actions)} tool call(s) failed ({names})"
            # Whether the work got done anyway is a different question from
            # whether a tool failed, and the loop made them come apart: a model
            # that is told a call failed can go round again and succeed another
            # way. Both facts are worth having, but claiming work was lost when
            # the agent went on to do it is its own kind of wrong.
            note += "." if state.get("results") else "; that work was not done."
            summary = (summary + " " if summary else "") + note

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
        """Approval first, then the loop, then done.

        The order matters. A pending proposal wins over any number of remaining
        iterations: the model does not get to keep going and quietly leave a
        human gate behind it.
        """
        pending = [p for p in state.get("proposals") or [] if p.is_pending]
        if pending:
            return "await_approval"

        context = state.get("context") or {}
        if (
            context.get("_path") == "model"
            and context.get("_acted")
            and state.get("iterations", 0) < spec.max_iterations
        ):
            return "reason"
        return "record"

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
        "act",
        route_after_act,
        {"await_approval": "await_approval", "reason": "reason", "record": "record"},
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


def _use_model(spec: AgentSpec, state: AgentState) -> bool:
    """Which planner runs: the live model, or the agent's deterministic path.

    The configured provider decides. This used to also prefer the deterministic
    path whenever an agent declared one — and all 13 do — so setting a real API
    key changed precisely nothing and the model was never asked anything.

    ``_force_path`` in the trigger payload pins a single run either way, which is
    how you compare what the model chose against what the deterministic path
    would have done for the same restaurant on the same day.
    """
    forced = (state.get("trigger_payload") or {}).get("_force_path")
    if forced == "model":
        return True
    if forced == "deterministic":
        return False
    return not llm.is_fake()


def _reason_with_model(spec: AgentSpec, state: AgentState) -> dict[str, Any]:
    """Live-model reasoning: bind the agent's tools and let it choose.

    Called once per turn round the act loop. The system prompt and the situation
    are rebuilt each time and the accumulated transcript — what it asked for,
    what came back — is appended, so on the second turn it is reading its own
    tool results rather than planning blind.
    """
    model = llm.get_model(spec.model_tier)
    tools = [_as_langchain_tool(t) for t in spec.tools]
    bound = model.bind_tools(tools) if tools else model

    messages: list[Any] = [
        SystemMessage(content=_system_prompt(spec)),
        HumanMessage(content=_context_prompt(state)),
        *(state.get("messages") or []),
    ]

    response = bound.invoke(messages)
    # The id matters: every tool_use block needs a tool_result quoting it back,
    # or the next request in the loop is rejected.
    calls = [
        {"name": call["name"], "args": call.get("args") or {}, "id": call.get("id")}
        for call in (getattr(response, "tool_calls", None) or [])
    ]

    update: dict[str, Any] = {
        "messages": [response],
        "iterations": state.get("iterations", 0) + 1,
        "context": {
            **(state.get("context") or {}),
            "_planned_calls": calls,
            "_path": "model",
        },
    }
    text = _message_text(response)
    if text:
        # Only overwrite the summary when there is something to overwrite it
        # with. A turn that just calls tools says nothing, and blanking the
        # summary there would throw away the sentence that explains the run.
        update["summary"] = text
    return update


def _system_prompt(spec: AgentSpec) -> str:
    """The agent's own brief, on top of who and where it is.

    Which restaurant, which timezone, which currency: settings the platform has
    always had and never told a model about. The order agent quoted a guest
    "$49.80" for a dish priced in ringgit, because nothing in the prompt or the
    context said otherwise and a bare number defaults to dollars.

    This belongs here rather than in the thirteen individual prompts. It is a
    property of the deployment, not of any one agent's job, and thirteen copies
    of it is thirteen chances to change twelve of them.
    """
    settings = get_settings()
    return (
        f"Your name is {spec.person}. You are working for "
        f"{settings.restaurant_name}, a restaurant operating in the "
        f"{settings.timezone} timezone.\n"
        f"All money is in {settings.currency}. Write amounts as "
        f"'{settings.currency} 24.90' — never with a currency symbol from "
        f"somewhere else.\n\n"
    ) + spec.system_prompt


def _message_text(response: Any) -> str:
    """The model's words, without its thinking.

    Claude Opus 5 thinks by default, so ``content`` arrives as a list of blocks
    and a plain ``str()`` over it dumps the raw block repr — chain of thought
    included — straight into the run summary, the audit trail, and whatever a
    human is shown in Slack.
    """
    accessor = getattr(response, "text", None)
    if isinstance(accessor, str):  # langchain's own text accessor, a str subclass
        return accessor.strip()

    content = getattr(response, "content", "")
    if isinstance(content, str):
        return content.strip()

    parts: list[str] = []
    for block in content or []:
        if isinstance(block, str):
            parts.append(block)
        elif isinstance(block, dict) and block.get("type") == "text":
            parts.append(str(block.get("text") or ""))
    return "\n".join(part for part in parts if part).strip()


# A single tool result is capped before it goes back to the model. Several tools
# sweep whole tables — every ingredient, every open ticket — so without this the
# size of the context, and the bill, is set by how much stock the restaurant
# happens to be carrying that morning.
_MAX_TOOL_RESULT_CHARS = 8000


def _render_result(result: dict[str, Any]) -> str:
    rendered = json.dumps(audit._jsonable(result), default=str)
    if len(rendered) <= _MAX_TOOL_RESULT_CHARS:
        return rendered
    return rendered[:_MAX_TOOL_RESULT_CHARS] + f"… [truncated; {len(rendered)} characters in full]"


def _reply(messages: list[ToolMessage], call: dict[str, Any], content: str) -> None:
    """Answer one tool call — when there is a model waiting to hear the answer.

    The deterministic path plans without call ids because nothing is listening,
    and manufacturing tool_result blocks for it would put messages in the
    transcript that no model ever asked for.
    """
    call_id = call.get("id")
    if not call_id:
        return
    messages.append(
        ToolMessage(
            content=content,
            tool_call_id=str(call_id),
            name=str(call.get("name") or ""),
        )
    )


def _context_prompt(state: AgentState) -> str:
    """Render what perceive loaded into the message the model actually sees."""
    context = {k: v for k, v in (state.get("context") or {}).items() if not k.startswith("_")}
    payload = {
        k: v for k, v in (state.get("trigger_payload") or {}).items() if not k.startswith("_")
    }

    parts = [
        f"Business date: {state['business_date']}",
        f"Trigger: {state.get('trigger')} ({state.get('trigger_ref') or 'n/a'})",
        "",
    ]
    if payload:
        # What actually caused this run: the guest's message, the ticket, the
        # webhook. perceive() loads the standing picture of the restaurant; this
        # is the request. It was missing, which left the order agent trying to
        # answer a guest whose question it had never been shown.
        parts += [
            "What triggered this run:",
            json.dumps(audit._jsonable(payload), indent=2, default=str),
            "",
        ]
    parts += [
        "Current situation:",
        json.dumps(audit._jsonable(context), indent=2, default=str),
        "",
        "Decide what to do and call the tools you need. If nothing needs doing, say so.",
    ]
    return "\n".join(parts)


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
