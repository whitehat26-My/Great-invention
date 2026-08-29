"""The shared agent framework.

Every agent is the same compiled LangGraph graph, differing only by its
AgentSpec: prompt, tools, model and approval policy. One framework, as many
configurations as there are agents.
"""

from restaurant_ai.kernel.spec import AgentSpec, ToolSpec
from restaurant_ai.kernel.state import AgentState, Proposal

__all__ = ["AgentSpec", "ToolSpec", "AgentState", "Proposal"]
