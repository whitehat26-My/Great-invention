"""The owner's question desk.

Until now the conversation ran one way. The agents write, the brief arrives at
23:55, cards appear with Approve and Reject — and the owner's only vocabulary is
two buttons. There was no way to ask *why*: why forty kilos of rice, how much
chicken is left, what tomorrow's roster looks like. The answer existed in the
database the whole time; nothing would say it out loud.

This is the other half of the conversation. A question in the approvals chat is
answered from the restaurant's own state, and an instruction — "restock the
kitchen", "build next week's roster" — is routed to the agent whose job it is,
in the same place the cards arrive, on the phone the owner already has in their
hand.

Two decisions shape it:

- **Read-only by construction, not by instruction.** The model is given no tools
  at all. Not a gated tool, not a tool it is told to avoid — none. A prompt that
  says "do not change anything" is a request; a model with nothing bound to it
  cannot write to this restaurant whatever it decides to do. Asking it to act
  gets an answer about who does that job and that it needs the owner's approval.
- **It reports what the agents see.** The snapshot is the agents' own ``perceive``
  views — the same reorder view Rain plans from, the same roster Henry builds
  against — so the answer and the agent that acts cannot disagree about the
  state of the restaurant. Like the brief, one broken view is a line, not a
  failure to answer.
- **An instruction is routed, never improvised.** Telling the desk to do
  something does not give this model the power to do it. It names the agent
  whose job it is and asks the owner to confirm; the agent then runs its own
  graph, with its own tools, behind the same approval gate as always. Routing
  that is not certain says so rather than guessing — a desk that acts on a
  coin-flip between two agents is worse than one that asks.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from typing import Any

from sqlalchemy.orm import Session

from restaurant_ai import clock
from restaurant_ai.config import get_settings
from restaurant_ai.logging_setup import get_logger

log = get_logger(__name__)

# A phone screen, and a Telegram message, are both small. The model is told to
# be brief; this is the backstop for when it is not.
_MAX_ANSWER_CHARS = 1200

# Each agent's view is capped before it reaches the prompt. The stock view alone
# sweeps every tracked ingredient, so without this the size of the context — and
# the bill for asking a one-line question — is set by how much stock the
# restaurant happens to be carrying.
_MAX_VIEW_CHARS = 2500


def build_snapshot(session: Session, business_date: date | None = None) -> dict[str, Any]:
    """Everything readable about the restaurant right now.

    One failed view is a line in the snapshot, not a failed answer: an owner
    asking about tomorrow's roster should still get it when the stock view is
    broken.
    """
    from restaurant_ai.brief import _perceive, build_brief
    from restaurant_ai.kernel.registry import all_agents

    day = business_date or clock.today()
    snapshot: dict[str, Any] = {"business_date": day.isoformat()}

    try:
        brief = build_brief(session, day)
        snapshot["today"] = brief.sections
        snapshot["needs_your_approval"] = brief.needs_you
        snapshot["failed_today"] = brief.failures
    except Exception as exc:
        snapshot["today"] = f"unavailable ({type(exc).__name__}: {exc})"

    views: dict[str, Any] = {}
    for name, spec in sorted(all_agents().items()):
        if spec.perceive is None:
            continue
        person = spec.person or name
        try:
            seen = _perceive(session, name, day)
        except Exception as exc:
            views[person] = f"unavailable ({type(exc).__name__}: {exc})"
            continue
        rendered = json.dumps(seen, default=str)
        if len(rendered) > _MAX_VIEW_CHARS:
            rendered = rendered[:_MAX_VIEW_CHARS] + f"… [truncated; {len(rendered)} chars in full]"
        views[person] = f"{spec.title}: {rendered}"
    snapshot["what_each_agent_sees"] = views

    return snapshot


def _system_prompt(snapshot: dict[str, Any]) -> str:
    settings = get_settings()
    return f"""You answer the owner's questions about {settings.restaurant_name}.

You are the question desk for a restaurant run by a team of AI agents. The owner
is asking you from their phone, in the same chat where approval cards arrive.

WHAT YOU CAN DO
Answer from the snapshot below, which is what the agents themselves currently
see. Money is in {settings.currency_symbol}. Times are {settings.timezone}.

WHAT YOU CANNOT DO
You cannot change anything — you have no tools, and nothing you say takes
effect. If the owner asks you to order stock, change a price, publish a post or
move a shift, say plainly that you cannot do it from here, name the agent whose
job it is, and remind them it arrives as a card to approve. Never imply that
something has been done.

HOW TO ANSWER
- Be short. This is read on a phone: a few sentences, not an essay.
- Use the numbers in the snapshot. Never invent one.
- If the snapshot does not contain the answer, say so and say what would.
- A view marked "unavailable" is broken, not empty — say that rather than
  reporting zero.
- Plain language a restaurant owner uses, not JSON field names.

THE RESTAURANT RIGHT NOW
{json.dumps(snapshot, indent=1, default=str)}"""


def answer(question: str, session: Session | None = None) -> str:
    """Answer one question about the restaurant. Never changes anything."""
    from restaurant_ai.db.base import session_scope
    from restaurant_ai.kernel import llm
    from restaurant_ai.kernel.graph import _message_text

    question = (question or "").strip()
    if not question:
        return "Ask me anything about the restaurant — stock, covers, the roster, today's numbers."

    if session is not None:
        snapshot = build_snapshot(session)
    else:
        with session_scope() as scoped:
            snapshot = build_snapshot(scoped)

    if llm.is_fake():
        # No model configured: say what is known rather than inventing prose.
        return _offline_answer(question, snapshot)

    from langchain_core.messages import HumanMessage, SystemMessage

    # No tools are bound. This model cannot act on the restaurant.
    model = llm.get_model("conversational")
    response = model.invoke(
        [SystemMessage(content=_system_prompt(snapshot)), HumanMessage(content=question)]
    )
    text = _message_text(response)
    if not text:
        return "I could not put an answer together just then. Ask me again?"
    if len(text) > _MAX_ANSWER_CHARS:
        text = text[:_MAX_ANSWER_CHARS].rstrip() + "…"
    return text


def _offline_answer(question: str, snapshot: dict[str, Any]) -> str:
    """What can be said without a model — the brief's own lines.

    Tests and unconfigured installs both land here. It answers with real numbers
    rather than a canned apology, because "no model configured" should not mean
    "no answer".
    """
    sections = snapshot.get("today")
    if not isinstance(sections, dict):
        return f"I cannot reach the restaurant's numbers right now ({sections})."

    lines = [f"No language model is configured, so here is today ({snapshot['business_date']}):"]
    for name, content in sections.items():
        if content:
            lines.append(f"{name}: {content[0]}")
    pending = snapshot.get("needs_your_approval") or []
    lines.append(
        f"{len(pending)} thing(s) waiting for your approval."
        if pending
        else "Nothing is waiting for your approval."
    )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Routing an instruction
# ---------------------------------------------------------------------------


@dataclass
class Intent:
    """What the owner wants, as far as the desk can tell.

    ``kind`` is one of ``question``, ``run`` or ``unclear``. Uncertainty is a
    first-class answer: a desk that guesses between two agents is worse than one
    that asks, because the owner finds out which it picked only afterwards.
    """

    kind: str
    agent: str | None = None
    reason: str = ""


def find_agent(text: str) -> str | None:
    """Match a name the owner typed to an agent slug, or nothing.

    Deterministic and model-free: this is what ``/run rain`` uses, so there is
    always one path to every agent that cannot be misread, misclassified, or
    knocked out by a rate limit.
    """
    from restaurant_ai.kernel.registry import all_agents

    wanted = (text or "").strip().lower().replace("-", "_").replace(" ", "_")
    if not wanted:
        return None
    for name, spec in all_agents().items():
        if wanted == name or wanted == (spec.person or "").lower():
            return name
    return None


def _agent_menu() -> str:
    from restaurant_ai.kernel.registry import all_agents

    return "\n".join(
        f"{name} — {spec.person}, {spec.title}: {spec.description}"
        for name, spec in sorted(all_agents().items())
    )


_ROUTER_PROMPT = """Decide what the owner of this restaurant wants.

Answer with exactly one line and nothing else — no explanation, no punctuation
beyond what is shown:

QUESTION
RUN <agent>
UNCLEAR

The agents you may name:
{menu}

Rules:
- Asking *about* the restaurant is QUESTION, even when it names an agent.
  "why did Rain order rice?" and "how much stock do we have?" are QUESTION.
- Telling you to *do* something an agent does is RUN.
  "restock the kitchen" is RUN stock_reorder. "build next week's roster" is
  RUN shift_scheduling.
- If two agents could both fit, or none clearly does, answer UNCLEAR.
  Never guess between two. The owner would rather be asked than surprised.
- Answer UNCLEAR for anything that is not about running this restaurant."""


def route(instruction: str) -> Intent:
    """Question, instruction, or neither — decided before anything happens.

    A model that is unreachable or rate-limited routes to ``unclear``, not to a
    guess: the deterministic ``/run <agent>`` path is what the owner falls back
    to, and it is named in the reply.
    """
    from restaurant_ai.kernel import llm
    from restaurant_ai.kernel.graph import _message_text

    text = (instruction or "").strip()
    if not text:
        return Intent(kind="unclear", reason="Nothing was said.")

    if llm.is_fake():
        # Without a model there is no classifier. Naming an agent still works,
        # because that path never needed one.
        named = find_agent(text)
        if named:
            return Intent(kind="run", agent=named, reason="you named the agent")
        return Intent(kind="question", reason="no model configured to route with")

    from langchain_core.messages import HumanMessage, SystemMessage

    try:
        model = llm.get_model("conversational")
        response = model.invoke(
            [
                SystemMessage(content=_ROUTER_PROMPT.format(menu=_agent_menu())),
                HumanMessage(content=text),
            ]
        )
        verdict = _message_text(response).strip().splitlines()[0].strip()
    except Exception as exc:
        log.warning("routing failed", error=str(exc))
        return Intent(kind="unclear", reason=f"I could not work out what you meant ({exc}).")

    if verdict.upper().startswith("QUESTION"):
        return Intent(kind="question")
    if verdict.upper().startswith("RUN"):
        named = find_agent(verdict.split(maxsplit=1)[1] if " " in verdict else "")
        if named:
            return Intent(kind="run", agent=named)
        # It answered RUN and then named something that is not an agent. That is
        # a misroute, and running the wrong agent is worse than asking.
        return Intent(kind="unclear", reason=f"I could not match “{verdict}” to an agent.")
    return Intent(kind="unclear", reason="I could not tell whether that was a question or a job.")
