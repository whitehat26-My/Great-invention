"""Running agents, and resuming the ones that stopped for a human.

Two entry points:

``run_agent``    starts a run. Returns when the agent finishes, or when it hits
                 an approval gate and parks.
``resume_agent`` picks up a parked run once someone has decided, and carries it
                 through to commit.

The checkpointer is what connects them. With Postgres behind it, the two calls
need not share a process, or even a deployment: an approval can be answered the
next morning by a webhook handler that knows nothing but the thread id.
"""

from __future__ import annotations

import contextlib
from contextlib import contextmanager
from datetime import date
from typing import Any

from langgraph.types import Command

from restaurant_ai import clock
from restaurant_ai.config import get_settings
from restaurant_ai.db.base import session_scope
from restaurant_ai.db.models import AgentRunStatus
from restaurant_ai.kernel import audit, llm
from restaurant_ai.kernel.graph import build_graph, format_exception
from restaurant_ai.kernel.spec import AgentSpec
from restaurant_ai.kernel.state import initial_state
from restaurant_ai.logging_setup import get_logger

log = get_logger(__name__)

_checkpointer_cm = None
_checkpointer = None
_compiled: dict[str, Any] = {}


def build_serde():
    """Checkpoint serialiser, with our state dataclasses explicitly allowed.

    The graph state carries Proposal and ActionRecord objects, so they get
    written into the checkpoint. LangGraph deserialises unregistered types with
    a warning today and will refuse to in a future version — which would break
    resuming a parked approval, the one thing the checkpointer exists for.
    Naming them here is also what makes the platform work under
    LANGGRAPH_STRICT_MSGPACK.
    """
    from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

    from restaurant_ai.kernel.state import ActionRecord, Proposal

    return JsonPlusSerializer(allowed_msgpack_modules=[Proposal, ActionRecord])


def get_checkpointer():
    """A process-wide Postgres checkpointer.

    Held open for the process lifetime rather than per-run: `setup()` creates
    the checkpoint tables and is not something to repeat on every agent call.
    """
    global _checkpointer_cm, _checkpointer
    if _checkpointer is not None:
        return _checkpointer

    from langgraph.checkpoint.postgres import PostgresSaver
    from psycopg import Connection
    from psycopg.rows import dict_row

    # Built by hand rather than via from_conn_string, which takes no serde.
    conn = Connection.connect(
        get_settings().psycopg_dsn, autocommit=True, prepare_threshold=0, row_factory=dict_row
    )
    _checkpointer_cm = conn
    _checkpointer = PostgresSaver(conn, serde=build_serde())
    _checkpointer.setup()
    return _checkpointer


def reset_checkpointer() -> None:
    """Drop the cached checkpointer. Used by tests and after a settings change."""
    global _checkpointer_cm, _checkpointer, _compiled
    if _checkpointer_cm is not None:
        with contextlib.suppress(Exception):
            _checkpointer_cm.close()
    _checkpointer_cm = None
    _checkpointer = None
    _compiled = {}


def get_compiled(spec: AgentSpec, checkpointer: Any | None = None):
    """Compile once per agent and reuse; graph construction is not free."""
    if checkpointer is not None:
        return build_graph(spec).compile(checkpointer=checkpointer)
    if spec.name not in _compiled:
        _compiled[spec.name] = build_graph(spec).compile(checkpointer=get_checkpointer())
    return _compiled[spec.name]


@contextmanager
def ephemeral_checkpointer():
    """An in-memory checkpointer, for tests that should not touch Postgres."""
    from langgraph.checkpoint.memory import InMemorySaver

    yield InMemorySaver(serde=build_serde())


class AgentOutcome:
    """What a run produced, and whether it is finished."""

    def __init__(
        self,
        run_id: str,
        thread_id: str,
        state: dict[str, Any],
        interrupted: bool,
        interrupt_payload: dict[str, Any] | None = None,
    ) -> None:
        self.run_id = run_id
        self.thread_id = thread_id
        self.state = state
        self.interrupted = interrupted
        self.interrupt_payload = interrupt_payload

    @property
    def summary(self) -> str:
        return self.state.get("summary") or ""

    @property
    def results(self) -> dict[str, Any]:
        return self.state.get("results") or {}

    @property
    def error(self) -> str | None:
        return self.state.get("error")

    @property
    def pending_proposals(self) -> list[Any]:
        return [p for p in (self.state.get("proposals") or []) if p.is_pending]

    def __repr__(self) -> str:  # pragma: no cover
        status = "awaiting-approval" if self.interrupted else "complete"
        return f"<AgentOutcome {self.run_id} {status}>"


def run_agent(
    spec: AgentSpec,
    business_date: date | None = None,
    trigger: str = "manual",
    trigger_ref: str | None = None,
    trigger_payload: dict[str, Any] | None = None,
    checkpointer: Any | None = None,
) -> AgentOutcome:
    """Start an agent. Parks at the approval gate if it proposes a gated action."""
    business_date = business_date or clock.today()
    thread_id = audit.new_thread_id(spec.name, business_date)

    settings = get_settings()
    # Ask the module that builds the client, rather than reading the Anthropic
    # settings and hoping. This read `settings.model_reasoning` whatever the
    # provider, so every Gemini run was filed in the audit trail as
    # `claude-opus-5` — the one column that answers "which model decided this?"
    # was wrong for any provider but the first one.
    model_name = "fake" if settings.llm_provider == "fake" else llm.model_name(spec.model_tier)

    with session_scope() as session:
        run = audit.start_run(
            session,
            agent_name=spec.name,
            department=spec.department,
            business_date=business_date,
            thread_id=thread_id,
            trigger=trigger,
            trigger_ref=trigger_ref,
            model=model_name,
            context={"trigger_payload": trigger_payload or {}},
        )
        run_id = run.id

    state = initial_state(
        agent_name=spec.name,
        department=spec.department,
        run_id=run_id,
        thread_id=thread_id,
        business_date=business_date,
        trigger=trigger,
        trigger_ref=trigger_ref,
        trigger_payload=trigger_payload,
    )

    graph = get_compiled(spec, checkpointer)
    config = {"configurable": {"thread_id": thread_id}}

    log.info("agent starting", agent=spec.name, run_id=run_id, trigger=trigger)
    try:
        result = graph.invoke(state, config)
    except Exception as exc:
        message = format_exception(exc)
        log.error("agent failed", agent=spec.name, run_id=run_id, error=message)
        with session_scope() as session:
            audit.finish_run(session, run_id, status=AgentRunStatus.FAILED, error=message)
        return AgentOutcome(run_id, thread_id, {"error": message}, interrupted=False)

    return _outcome(run_id, thread_id, result, spec)


def resume_agent(
    spec: AgentSpec, thread_id: str, decision: Any, checkpointer: Any | None = None
) -> AgentOutcome:
    """Continue a run that stopped for approval.

    ``decision`` is passed to ``apply_decision``, which accepts a bool, a
    verdict string, or a mapping — whatever the Slack handler, Telegram
    callback or CLI produced.
    """
    graph = get_compiled(spec, checkpointer)
    config = {"configurable": {"thread_id": thread_id}}

    with session_scope() as session:
        from sqlalchemy import select

        from restaurant_ai.db.models import AgentRun

        run = session.execute(
            select(AgentRun).where(AgentRun.thread_id == thread_id)
        ).scalar_one_or_none()
        run_id = run.id if run else ""

    log.info("agent resuming", agent=spec.name, thread_id=thread_id)
    try:
        result = graph.invoke(Command(resume=decision), config)
    except Exception as exc:
        message = format_exception(exc)
        log.error("resume failed", agent=spec.name, thread_id=thread_id, error=message)
        if run_id:
            with session_scope() as session:
                audit.finish_run(session, run_id, status=AgentRunStatus.FAILED, error=message)
        return AgentOutcome(run_id, thread_id, {"error": message}, interrupted=False)

    return _outcome(run_id, thread_id, result, spec)


def _outcome(run_id: str, thread_id: str, result: dict[str, Any], spec: AgentSpec) -> AgentOutcome:
    interrupts = result.get("__interrupt__") or []
    if interrupts:
        payload = getattr(interrupts[0], "value", None)
        log.info("agent awaiting approval", agent=spec.name, run_id=run_id)
        return AgentOutcome(run_id, thread_id, result, interrupted=True, interrupt_payload=payload)

    log.info(
        "agent complete",
        agent=spec.name,
        run_id=run_id,
        summary=(result.get("summary") or "")[:120],
    )
    return AgentOutcome(run_id, thread_id, result, interrupted=False)
