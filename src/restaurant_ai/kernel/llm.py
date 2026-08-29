"""Model selection, and the deterministic stand-in.

``LLM_PROVIDER=fake`` swaps in a scripted model so the entire platform — every
agent, the simulated service day, the whole test suite — runs with no API key
and no network. That is not a testing convenience bolted on afterwards: an
operations platform whose behaviour can only be observed by spending money on
inference is one nobody can safely change.

The fake does not pretend to reason. Agents that need real judgement declare an
``autonomous`` path in their spec, which the graph prefers whenever the fake is
active, so what runs offline is the deterministic logic rather than a
hallucinated imitation of it.

Two live providers are supported, ``anthropic`` and ``google``. Everything above
this module is provider-agnostic — the graph, the loop, the tool dispatch and
the approval gate all sit on LangChain's ``BaseChatModel`` — so the differences
are confined here, and they are real ones:

- Claude Opus 5 and Sonnet 5 **reject** a request carrying ``temperature``.
  Gemini requires one or defaults to 0.7.
- Claude takes ``thinking={"type": "adaptive"}``. Gemini 3 dropped
  ``thinking_budget`` in favour of a thinking *level*.

Neither of those is something a shared setting can paper over, so each provider
reads the settings that apply to it and ignores the ones that do not.
"""

from __future__ import annotations

from typing import Any

from langchain_core.language_models import BaseChatModel

from restaurant_ai.config import get_settings
from restaurant_ai.logging_setup import get_logger

log = get_logger(__name__)

_cache: dict[str, BaseChatModel] = {}


class FakeModelInUse(RuntimeError):
    """Raised when live-model behaviour is required but the fake is configured."""


def is_fake(interactive: bool = False) -> bool:
    return provider_for(interactive) == "fake"


def provider_for(interactive: bool = False) -> str:
    """Who answers this call.

    One provider unless the deployment splits them. The split is by *who is
    waiting*, not by tier: a scheduled run can take three minutes on a machine
    under the counter and cost nothing, while the same three minutes on the
    owner's question is a bot that looks dead.
    """
    settings = get_settings()
    if interactive and settings.llm_provider_interactive:
        return str(settings.llm_provider_interactive)
    return str(settings.llm_provider)


def model_name(tier: str = "conversational", *, interactive: bool = False) -> str:
    """The model id for a tier under whichever provider answers this call."""
    settings = get_settings()
    reasoning = tier == "reasoning"
    provider = provider_for(interactive)
    if provider == "google":
        return (
            settings.google_model_reasoning if reasoning else settings.google_model_conversational
        )
    if provider == "ollama":
        return (
            settings.ollama_model_reasoning if reasoning else settings.ollama_model_conversational
        )
    return settings.model_reasoning if reasoning else settings.model_conversational


def get_model(tier: str = "conversational", *, interactive: bool = False) -> BaseChatModel:
    """Return the chat model for a tier.

    ``reasoning`` gets the stronger model for the agents doing analysis
    (pricing, reconciliation, performance); ``conversational`` gets the faster
    one for high-volume guest-facing work.

    ``interactive`` is for anything a person is waiting on. The default budget
    is ten retries with no deadline, which is correct for a scheduled run —
    better to wait out a rate limit than fail the nightly close. On a chat it is
    the wrong trade entirely: the caller sits in backoff for minutes saying
    nothing, and silence is indistinguishable from a dead bot. Interactive
    callers get one retry and a deadline, and a failure they can report.
    """
    settings = get_settings()
    provider = provider_for(interactive)
    if provider == "fake":
        raise FakeModelInUse(
            "LLM_PROVIDER=fake: no chat model is available. Agents fall back to "
            "their deterministic path; set LLM_PROVIDER=anthropic to use a model."
        )

    name = model_name(tier, interactive=interactive)
    # Keyed by provider too. Model ids are not unique across providers in
    # principle, and a cache that assumes they are hands back the wrong client
    # the first time they collide.
    key = f"{provider}:{name}:{'chat' if interactive else 'batch'}"
    if key in _cache:
        return _cache[key]

    builder = {
        "anthropic": _build_anthropic,
        "google": _build_google,
        "ollama": _build_ollama,
    }[provider]
    model = builder(settings, name, interactive)
    log.info("model initialised", tier=tier, provider=provider, model=name)
    _cache[key] = model
    return model


def _patience(settings: Any, interactive: bool) -> dict[str, Any]:
    """How long to keep trying, and whether to give up at all."""
    if interactive:
        return {
            "max_retries": settings.llm_interactive_max_retries,
            "timeout": settings.llm_interactive_timeout,
        }
    return {"max_retries": settings.llm_max_retries}


def _build_anthropic(settings: Any, name: str, interactive: bool = False) -> BaseChatModel:
    if not settings.anthropic_api_key:
        raise RuntimeError(
            "LLM_PROVIDER=anthropic but ANTHROPIC_API_KEY is not set. "
            "Set the key, or use LLM_PROVIDER=fake to run deterministically."
        )

    from langchain_anthropic import ChatAnthropic
    from pydantic import SecretStr

    kwargs: dict[str, Any] = {
        "model": name,
        "api_key": SecretStr(settings.anthropic_api_key),
        "max_tokens": settings.llm_max_tokens,
        **_patience(settings, interactive),
    }
    # Only send a sampling parameter if one was actually asked for. Claude
    # Opus 5 and Sonnet 5 reject the request outright if `temperature` is
    # present at all, so a defaulted 0.0 is not a harmless no-op — it is a 400
    # on every single call the platform would ever make.
    if settings.llm_temperature is not None:
        kwargs["temperature"] = settings.llm_temperature
    if settings.llm_thinking != "off":
        # Adaptive: the model decides how hard to think per request. That suits
        # both tiers — "what is on the menu" costs nothing extra, while "does
        # this dish contain peanuts" gets the thought it deserves.
        kwargs["thinking"] = {"type": settings.llm_thinking}

    # langchain-anthropic's stubs disagree with its runtime signature here
    # (model / max_tokens are accepted; api_key coerces a str). Verified working
    # against the installed version.
    return ChatAnthropic(**kwargs)  # type: ignore[arg-type]


def _build_google(settings: Any, name: str, interactive: bool = False) -> BaseChatModel:
    if not settings.google_api_key:
        raise RuntimeError(
            "LLM_PROVIDER=google but GOOGLE_API_KEY is not set. Get a free key at "
            "https://aistudio.google.com, or use LLM_PROVIDER=fake to run "
            "deterministically."
        )

    from langchain_google_genai import ChatGoogleGenerativeAI
    from pydantic import SecretStr

    kwargs: dict[str, Any] = {
        "model": name,
        "google_api_key": SecretStr(settings.google_api_key),
        "max_output_tokens": settings.llm_max_tokens,
        **_patience(settings, interactive),
    }
    # Same rule as Anthropic, for the same reason: Gemini 3 Flash uses fixed
    # sampling and discards a temperature it is sent, warning once per call
    # while it does so. Both frontier providers have landed in the same place.
    if settings.llm_temperature is not None:
        kwargs["temperature"] = settings.llm_temperature
    # Gemini 3 replaced `thinking_budget` with a thinking level, which
    # langchain exposes as `reasoning_effort`. Left unset the model uses its own
    # default, which is the right starting point — LLM_THINKING is Anthropic's
    # spelling and does not carry over.
    if settings.google_reasoning_effort is not None:
        kwargs["reasoning_effort"] = settings.google_reasoning_effort

    return ChatGoogleGenerativeAI(**kwargs)


def _build_ollama(settings: Any, name: str, interactive: bool = False) -> BaseChatModel:
    """A model running on this machine. No key, no quota, no bill.

    What it costs instead is hardware and patience. An 8B model quantised wants
    about 5GB of RAM and answers in minutes on a CPU where a hosted model takes
    seconds — which is why this belongs on the scheduled side of the split and
    the owner's chat usually does not.

    The agents bind real tools, so the model has to be one that can call them.
    Hermes is trained for it and is among the better open models at it; a base
    chat model that cannot emit a tool call will run the loop to its iteration
    limit doing nothing, and report that it ran out of turns.

    ``num_ctx`` is the one setting that silently ruins results. Ollama defaults
    to a small context — smaller than one agent's system prompt plus its tool
    schemas plus a day of situation — and it does not error when the prompt is
    too long, it *discards the front of it*. The agent then reasons without its
    instructions and reports confidently on nothing.
    """
    from langchain_ollama import ChatOllama

    kwargs: dict[str, Any] = {
        "model": name,
        "base_url": settings.ollama_host,
        "num_ctx": settings.ollama_context,
        "num_predict": settings.llm_max_tokens,
        # Otherwise nearly every scheduled run pays to load 5GB from disk again,
        # because they are an hour apart and Ollama forgets in minutes.
        "keep_alive": settings.ollama_keep_alive,
        # Retries here are a client-side loop against a server on this machine.
        # A rate limit is impossible; the failures are "not running" and "model
        # not pulled", and retrying those ten times only delays the message
        # that says which.
        "max_retries": 1,
    }
    if interactive:
        kwargs["timeout"] = settings.llm_interactive_timeout
    if settings.llm_temperature is not None:
        kwargs["temperature"] = settings.llm_temperature
    return ChatOllama(**kwargs)


def available_models() -> list[str]:
    """What the configured key can actually see.

    Model ids move — Flash went 3.0 to 3.7 inside a year — and the ``-latest``
    aliases are not safe to pin to; one of them resolved to a deprecated model
    and returned a bare 404. This turns "that id is wrong" from a mystery into
    a list.
    """
    provider = get_settings().llm_provider
    if provider == "google":
        return _google_models()
    if provider == "anthropic":
        return _anthropic_models()
    if provider == "ollama":
        return _ollama_models()
    raise FakeModelInUse("LLM_PROVIDER=fake: there are no models to list.")


def _ollama_models() -> list[str]:
    """What is actually pulled onto this machine.

    A model named in .env but never pulled is the likeliest local failure, and
    it is indistinguishable from a typo until something lists what is there.
    """
    import httpx

    host = get_settings().ollama_host.rstrip("/")
    try:
        response = httpx.get(f"{host}/api/tags", timeout=10)
        response.raise_for_status()
    except Exception as exc:
        raise RuntimeError(
            f"Could not reach Ollama at {host} ({exc}). Start it with `ollama serve`, "
            "or install it from https://ollama.com."
        ) from exc
    return sorted(m["name"] for m in response.json().get("models", []))


def _google_models() -> list[str]:
    from google import genai

    client = genai.Client(api_key=get_settings().google_api_key)
    names = []
    for model in client.models.list():
        actions = getattr(model, "supported_actions", None)
        if actions and "generateContent" not in actions:
            continue
        if model.name:
            names.append(model.name.removeprefix("models/"))
    return sorted(names)


def _anthropic_models() -> list[str]:
    import anthropic

    client = anthropic.Anthropic(api_key=get_settings().anthropic_api_key)
    return sorted(model.id for model in client.models.list())


def reset_model_cache() -> None:
    _cache.clear()


def describe_provider() -> dict[str, Any]:
    settings = get_settings()
    described = {
        "provider": settings.llm_provider,
        "reasoning": model_name("reasoning"),
        "conversational": model_name("conversational"),
        "max_tokens": settings.llm_max_tokens,
    }
    # Report the knob that actually applies, rather than the one this provider
    # ignores.
    if settings.llm_provider == "google":
        described["thinking"] = settings.google_reasoning_effort or "model default"
        described["has_key"] = bool(settings.google_api_key)
    elif settings.llm_provider == "ollama":
        # There is no key to have, and saying "no key" would read as a fault.
        described["thinking"] = "model default"
        described["has_key"] = True
        described["host"] = settings.ollama_host
    else:
        described["thinking"] = settings.llm_thinking
        described["has_key"] = bool(settings.anthropic_api_key)

    # Only when the deployment actually splits them. Reporting one provider
    # twice on every other machine is noise.
    interactive = provider_for(interactive=True)
    if interactive != settings.llm_provider:
        described["interactive_provider"] = interactive
        described["interactive"] = model_name("conversational", interactive=True)
    return described
