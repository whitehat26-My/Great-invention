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


def is_fake() -> bool:
    return get_settings().llm_provider == "fake"


def get_model(tier: str = "conversational") -> BaseChatModel:
    """Return the chat model for a tier.

    ``reasoning`` gets the stronger model for the agents doing analysis
    (pricing, reconciliation, performance); ``conversational`` gets the faster
    one for high-volume guest-facing work.
    """
    settings = get_settings()
    if settings.llm_provider == "fake":
        raise FakeModelInUse(
            "LLM_PROVIDER=fake: no chat model is available. Agents should use their "
            "autonomous path, which the kernel selects automatically."
        )

    model_name = settings.model_reasoning if tier == "reasoning" else settings.model_conversational
    if model_name in _cache:
        return _cache[model_name]

    if not settings.anthropic_api_key:
        raise RuntimeError(
            "LLM_PROVIDER=anthropic but ANTHROPIC_API_KEY is not set. "
            "Set the key, or use LLM_PROVIDER=fake to run deterministically."
        )

    from langchain_anthropic import ChatAnthropic
    from pydantic import SecretStr

    # langchain-anthropic's stubs disagree with its runtime signature here
    # (model / max_tokens are accepted; api_key coerces a str). Verified working
    # against the installed version.
    model = ChatAnthropic(  # type: ignore[call-arg]
        model=model_name,
        api_key=SecretStr(settings.anthropic_api_key),
        max_tokens=settings.llm_max_tokens,
        temperature=settings.llm_temperature,
    )
    log.info("model initialised", tier=tier, model=model_name)
    _cache[model_name] = model
    return model


def reset_model_cache() -> None:
    _cache.clear()


def describe_provider() -> dict[str, Any]:
    settings = get_settings()
    return {
        "provider": settings.llm_provider,
        "reasoning": settings.model_reasoning,
        "conversational": settings.model_conversational,
        "has_key": bool(settings.anthropic_api_key),
    }
