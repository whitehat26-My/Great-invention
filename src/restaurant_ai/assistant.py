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

WHO YOU ARE
Your name is Keanu. You work here. The owner is a colleague you talk to every
day, not a user filing a query, so talk the way a trusted manager does on the
phone: warm, direct, and brief. Use "we" about the restaurant — it is yours too.
Malay or English, whichever they use; mixing them is normal here and fine.

HOW TO ANSWER
- Be short. This is read on a phone: a few sentences, not an essay.
- Answer in the flow of the conversation. If they have just asked about chicken
  and then say "and rice?", that is about rice stock — do not ask them to
  repeat themselves.
- Use the numbers in the snapshot. Never invent one.
- If the snapshot does not contain the answer, say so and say what would.
- A view marked "unavailable" is broken, not empty — say that rather than
  reporting zero.
- Plain language a restaurant owner uses, not JSON field names.
- No greeting every time, no "I hope this helps", no restating their question
  back at them. Answer, and stop.
- Say what you think when it matters. "Rice is fine, but the chicken will not
  last the weekend" is worth more than either number on its own.

THE RESTAURANT RIGHT NOW
{json.dumps(snapshot, indent=1, default=str)}"""


def answer(
    question: str,
    session: Session | None = None,
    history: list[Any] | None = None,
) -> str:
    """Answer one question about the restaurant. Never changes anything.

    ``history`` is the recent exchange, oldest first, so a follow-up is read as
    a follow-up. Without it every message arrived alone and "and rice?" was
    unanswerable — which is the difference between a search box and a colleague.
    """
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

    if llm.is_fake(interactive=True):
        # No model configured: say what is known rather than inventing prose.
        return _offline_answer(question, snapshot)

    from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

    from restaurant_ai.memory import KEANU

    # No tools are bound. This model cannot act on the restaurant.
    conversation: list[Any] = [SystemMessage(content=_system_prompt(snapshot))]
    for turn in history or []:
        role = getattr(turn, "role", None)
        text = getattr(turn, "text", "")
        if not text:
            continue
        conversation.append(
            AIMessage(content=text) if role == KEANU else HumanMessage(content=text)
        )
    conversation.append(HumanMessage(content=question))

    try:
        model = llm.get_model("conversational", interactive=True)
        response = model.invoke(conversation)
    except Exception as exc:
        log.warning("answering failed", error=str(exc))
        return explain_model_failure(exc)
    text = _message_text(response)
    if not text:
        return "I could not put an answer together just then. Ask me again?"
    if len(text) > _MAX_ANSWER_CHARS:
        text = text[:_MAX_ANSWER_CHARS].rstrip() + "…"
    return text


def explain_model_failure(exc: Exception) -> str:
    """A provider error, in words the owner can do something about.

    "ResourceExhausted: 429" is the model's language, not the restaurant's, and
    the three failures that actually happen have three different answers: wait
    until tomorrow, check the network, or nothing is wrong and it was just slow.
    """
    detail = str(exc)
    lowered = detail.lower()

    # Not a provider failure at all: the client library for this provider is not
    # installed. It reads like one because it surfaces from the same call, and
    # every other message here would send the owner to check a key, a network or
    # a quota that has nothing to do with it.
    #
    # It happens on exactly one path, and that path is the common one: a `git
    # pull` brings code that needs a package the environment does not have, and
    # nothing installs it. Adding any future provider adds a dependency, so this
    # is not specific to the one that revealed it.
    if isinstance(exc, ModuleNotFoundError) or "no module named" in lowered:
        missing = getattr(exc, "name", "") or "a provider package"
        return (
            f"The library for this provider is not installed ({missing}).\n\n"
            "Nothing is wrong with the model, the key or the network — the code was "
            "updated and the packages were not. From the project folder, in the "
            "virtualenv:\n\n"
            "    pip install -e ."
        )

    # A local model fails in ways a hosted one cannot, and shares vocabulary
    # with ways it can. "Timed out" from Ollama is a CPU thinking, not a spent
    # quota — so these are answered first or the advice below is confidently
    # wrong about a machine that has no quota to spend.
    from restaurant_ai.kernel import llm

    if llm.provider_for() == "ollama" or llm.provider_for(interactive=True) == "ollama":
        host = get_settings().ollama_host
        if "connection" in lowered or "refused" in lowered or "connect" in lowered:
            return (
                f"The local model is not answering at {host}.\n\n"
                "Ollama is either not installed or not running. Open it from the Start "
                "menu, or check with `ollama list` in a new terminal — a window opened "
                "before Ollama was installed will not have found it yet."
            )
        if "not found" in lowered or "no such model" in lowered or "try pulling" in lowered:
            return (
                "That model is not on this machine yet. Pull it once with "
                "`ollama pull hermes3` — about 5GB, and it only happens the first "
                "time.\n\n"
                "`restaurant-ai models` lists what is already here."
            )
        if "memory" in lowered or "out of memory" in lowered:
            return (
                "This machine does not have enough free memory for that model. A "
                "smaller one fits: `ollama pull hermes3:3b`, then set "
                "OLLAMA_MODEL_REASONING and OLLAMA_MODEL_CONVERSATIONAL to it in .env."
            )
        if "timeout" in lowered or "timed out" in lowered or "deadline" in lowered:
            # No quota exists locally, so the hosted explanation below would be
            # nonsense here. On a CPU this is simply how long it takes.
            return (
                "The local model did not finish in time. On a CPU that is normal rather "
                "than broken — an answer can take minutes.\n\n"
                "For questions you wait on, LLM_PROVIDER_INTERACTIVE=anthropic in .env "
                "sends just the chat to a hosted model and leaves the scheduled agents "
                "running free on this machine."
            )

    if "429" in detail or "quota" in lowered or "resource_exhausted" in lowered:
        return (
            "I have used up today's free quota with the language model, so I cannot "
            "think about that until it resets.\n\n"
            "/run <name>, /brief, /pending and /agents all still work — none of them "
            "need the model.\n\n"
            "The free tier counts per model, so pointing "
            "GOOGLE_MODEL_CONVERSATIONAL at a different one in .env buys another day's "
            "worth immediately."
        )
    if "timeout" in lowered or "deadline" in lowered or "timed out" in lowered:
        # Reported as slowness, and usually is not. A spent free tier answers
        # 429 with "retry in 45s", which outlasts any deadline a person waiting
        # on a chat would accept — so the timeout is the quota in disguise, and
        # saying only "it was slow" sends the owner to check their wifi.
        return (
            "The language model did not answer in time. On the free tier that "
            "usually means the day's quota is spent — it asks for a 45-second "
            "wait, which is longer than I hold on for.\n\n"
            "`restaurant-ai doctor` says which it is. /run <name>, /brief, "
            "/pending and /agents need no model at all."
        )
    if "api key" in lowered or "unauthenticated" in lowered or "permission" in lowered:
        return (
            "The language model refused my key. Check GOOGLE_API_KEY (or "
            "ANTHROPIC_API_KEY) in .env — `restaurant-ai doctor` will confirm it."
        )
    return f"I could not reach the language model ({type(exc).__name__}: {detail[:110]})."


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

    ``kind`` is one of ``question``, ``run``, ``greeting`` or ``unclear``. Uncertainty is a
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

    # Docs write `/run <name>`, and people paste it exactly, brackets and all.
    # Refusing "<Irma>" for a name we plainly have is pedantry, not precision.
    wanted = (text or "").strip().strip("<>[]{}\"'").strip()
    wanted = wanted.lower().replace("-", "_").replace(" ", "_")
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


def _first_meaningful_line(text: str) -> str:
    """The verdict, past whatever the model dressed it in.

    The contract asks for one bare word and models answer it in markdown:
    ``**QUESTION**``, a fenced block, a leading blank line from a stripped
    thinking block. Matching on the raw first line failed all three, and a
    formatting difference is not a reason to refuse the owner an answer.
    """
    for line in text.splitlines():
        cleaned = line.strip().strip("`*_#>-—– \t").strip("\"'")
        # "Answer: RUN rain" and "Verdict — QUESTION" both happen.
        for lead in ("answer:", "verdict:", "intent:", "classification:"):
            if cleaned.lower().startswith(lead):
                cleaned = cleaned[len(lead) :].strip()
        if cleaned:
            return cleaned
    return ""


# Said to a bot by everyone, meaning nothing, and costing two model calls out of
# a free tier's twenty a day if it goes to the router and then the desk.
_PLEASANTRIES = {
    "hi",
    "hey",
    "hello",
    "yo",
    "helo",
    "hai",
    "halo",
    "thanks",
    "thank you",
    "ty",
    "terima kasih",
    "tq",
    "ok",
    "okay",
    "oki",
    "sip",
    "good",
    "nice",
    "cool",
    "morning",
    "good morning",
    "good night",
    "night",
    "gn",
}


def is_pleasantry(text: str) -> bool:
    """Whether this is a greeting rather than a question about the restaurant."""
    cleaned = (text or "").strip().strip("!.?,").lower()
    return cleaned in _PLEASANTRIES


def greet() -> str:
    """A reply that costs nothing and still says what can be done."""
    return (
        "I am here. Ask me about the restaurant — stock, covers, the roster — "
        "or tell me what to do.\n\n"
        "/agents lists who works here. /help shows everything."
    )


def route(instruction: str, history: list[Any] | None = None) -> Intent:
    """Question, instruction, or neither — decided before anything happens.

    A model that is unreachable or rate-limited routes to ``unclear``, not to a
    guess: the deterministic ``/run <agent>`` path is what the owner falls back
    to, and it is named in the reply.

    ``history`` is what makes short replies routable. "do it" is an instruction
    only if you remember being asked "should we reorder the rice?" a moment ago;
    read alone it is not routable at all, and the owner gets asked to repeat
    themselves in the one situation where they were being clearest.
    """
    from restaurant_ai.kernel import llm
    from restaurant_ai.kernel.graph import _message_text

    text = (instruction or "").strip()
    if not text:
        return Intent(kind="unclear", reason="Nothing was said.")
    if is_pleasantry(text):
        return Intent(kind="greeting")

    if llm.is_fake(interactive=True):
        # Without a model there is no classifier. Naming an agent still works,
        # because that path never needed one.
        named = find_agent(text)
        if named:
            return Intent(kind="run", agent=named, reason="you named the agent")
        return Intent(kind="question", reason="no model configured to route with")

    from langchain_core.messages import HumanMessage, SystemMessage

    asked: list[Any] = [SystemMessage(content=_ROUTER_PROMPT.format(menu=_agent_menu()))]
    if history:
        # As context for the classification, not as more to classify: only the
        # last line is being decided, and saying so stops the router answering
        # about a message two turns back.
        said = "\n".join(f"{t.role}: {t.text}" for t in history if getattr(t, "text", ""))
        if said:
            asked.append(
                HumanMessage(
                    content=(
                        f"Recent conversation, for context only:\n{said}\n\n"
                        "Classify only the next message."
                    )
                )
            )
    asked.append(HumanMessage(content=text))

    try:
        model = llm.get_model("conversational", interactive=True)
        response = model.invoke(asked)
        verdict = _first_meaningful_line(_message_text(response))
    except Exception as exc:
        log.warning("routing failed", error=str(exc))
        return Intent(kind="unclear", reason=explain_model_failure(exc))

    upper = verdict.upper()
    if upper.startswith("QUESTION"):
        return Intent(kind="question")
    if upper.startswith("RUN"):
        named = find_agent(verdict.split(maxsplit=1)[1] if " " in verdict else "")
        if named:
            return Intent(kind="run", agent=named)
        # It answered RUN and then named something that is not an agent. That is
        # a misroute, and running the wrong agent is worse than asking.
        return Intent(kind="unclear", reason=f"I could not match “{verdict}” to an agent.")
    if upper.startswith("UNCLEAR"):
        # The model's own judgement that it cannot tell. Asking is right.
        return Intent(
            kind="unclear", reason="I could not tell whether that was a question or a job."
        )

    # It said something we do not recognise at all. Answering is the safe guess
    # and refusing is not: the two mistakes are not symmetric. Treating an
    # instruction as a question costs a reply explaining whose job it is —
    # useful in itself. Treating a question as an instruction runs an agent
    # nobody asked for. So an unreadable verdict answers, and says so in the log
    # rather than at the owner, who did nothing wrong.
    log.warning(
        "router said something unrecognised; answering as a question", verdict=verdict[:120]
    )
    return Intent(kind="question")
