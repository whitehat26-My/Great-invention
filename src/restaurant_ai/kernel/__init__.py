"""The shared agent framework.

Every one of the 13 agents is the same compiled LangGraph graph, differing only
by its AgentSpec: prompt, tools, model and approval policy. One framework,
thirteen configurations.
"""

from restaurant_ai.kernel.spec import AgentSpec, ToolSpec
from restaurant_ai.kernel.state import AgentState, Proposal

__all__ = ["AgentSpec", "ToolSpec", "AgentState", "Proposal"]
