"""The agent registry.

Every agent is declared once, here. The CLI, the Celery tasks, the API routes
and the simulator all resolve agents through this, so adding a fourteenth agent
means adding one entry rather than touching four call sites.

Agent modules are imported lazily inside ``_load`` to keep the import graph
acyclic: agent modules import the kernel, so the kernel cannot import them at
module scope.
"""

from __future__ import annotations

from restaurant_ai.kernel.spec import AgentSpec

_registry: dict[str, AgentSpec] = {}
_loaded = False


def register(spec: AgentSpec) -> AgentSpec:
    if spec.name in _registry:
        raise ValueError(f"Agent {spec.name!r} is already registered")
    _registry[spec.name] = spec
    return spec


def _load() -> None:
    global _loaded
    if _loaded:
        return
    _loaded = True
    # Importing each module registers its agent(s) as a side effect.
    from restaurant_ai.agents import (  # noqa: F401
        finance,
        front_of_house,
        kitchen,
        marketing,
        supply,
        workforce,
    )


def all_agents() -> dict[str, AgentSpec]:
    _load()
    return dict(_registry)


def get_agent(name: str) -> AgentSpec:
    _load()
    if name not in _registry:
        available = ", ".join(sorted(_registry)) or "none"
        raise KeyError(f"Unknown agent {name!r}. Registered: {available}")
    return _registry[name]


def agents_in(department: str) -> list[AgentSpec]:
    _load()
    return [s for s in _registry.values() if s.department == department]


def departments() -> list[str]:
    _load()
    return sorted({s.department for s in _registry.values()})


def reset_registry() -> None:
    """Test hook: clear the registry so modules can be re-imported."""
    global _loaded
    _registry.clear()
    _loaded = False
