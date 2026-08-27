"""The live-model path.

The whole platform runs on the deterministic path by default, which means the
LLM wiring could rot without any test noticing until someone sets a real key.
These exercise everything up to the network boundary: provider selection, model
construction, tool binding and the JSON schemas the model is shown.

Making an actual inference call needs a key and is the one verification step
that cannot run here.
"""

from __future__ import annotations

import pytest

from restaurant_ai.config import get_settings, reset_settings_cache
from restaurant_ai.kernel import llm
from restaurant_ai.kernel.graph import _as_langchain_tool, _context_prompt
from restaurant_ai.kernel.registry import all_agents, get_agent


@pytest.fixture
def anthropic_env(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "anthropic")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-not-a-real-key")
    reset_settings_cache()
    llm.reset_model_cache()
    yield
    reset_settings_cache()
    llm.reset_model_cache()


class TestProviderSelection:
    def test_fake_is_the_default(self):
        assert get_settings().llm_provider == "fake"
        assert llm.is_fake()

    def test_the_fake_provider_refuses_to_hand_out_a_model(self):
        # Agents must take their deterministic path rather than silently
        # getting a model that would try to reach the network.
        with pytest.raises(llm.FakeModelInUse):
            llm.get_model("reasoning")

    def test_a_missing_key_fails_loudly(self, monkeypatch):
        monkeypatch.setenv("LLM_PROVIDER", "anthropic")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "")
        reset_settings_cache()
        llm.reset_model_cache()
        with pytest.raises(RuntimeError, match="ANTHROPIC_API_KEY"):
            llm.get_model("reasoning")
        reset_settings_cache()

    def test_describe_provider(self):
        described = llm.describe_provider()
        assert described["provider"] == "fake"
        assert described["reasoning"] and described["conversational"]


class TestModelConstruction:
    def test_reasoning_and_conversational_tiers_differ(self, anthropic_env):
        assert llm.get_model("reasoning").model != llm.get_model("conversational").model

    def test_the_configured_models_are_used(self, anthropic_env):
        settings = get_settings()
        assert llm.get_model("reasoning").model == settings.model_reasoning
        assert llm.get_model("conversational").model == settings.model_conversational

    def test_models_are_cached(self, anthropic_env):
        assert llm.get_model("reasoning") is llm.get_model("reasoning")


class TestToolBinding:
    @pytest.mark.parametrize("name", sorted(all_agents()))
    def test_every_agents_tools_bind(self, anthropic_env, name):
        # A malformed args_schema would only surface on the first live run.
        spec = get_agent(name)
        model = llm.get_model(spec.model_tier)
        tools = [_as_langchain_tool(t) for t in spec.tools]
        model.bind_tools(tools) if tools else None

    @pytest.mark.parametrize("name", sorted(all_agents()))
    def test_every_tool_exposes_a_json_schema(self, name):
        for tool in get_agent(name).tools:
            langchain_tool = _as_langchain_tool(tool)
            schema = langchain_tool.args_schema.model_json_schema()
            assert "properties" in schema
            assert langchain_tool.description

    def test_tool_arguments_are_described(self):
        # The model chooses arguments from these descriptions.
        tool = _as_langchain_tool(get_agent("ordering").tool("check_allergens"))
        properties = tool.args_schema.model_json_schema()["properties"]
        assert "sku" in properties and "allergens" in properties
        assert all("description" in p for p in properties.values())


class TestPromptAssembly:
    def test_the_context_prompt_carries_what_perceive_found(self):
        from datetime import date

        state = {
            "business_date": date(2026, 8, 27),
            "trigger": "schedule",
            "trigger_ref": "07:00 sweep",
            "context": {"below_reorder_point": 4, "_planned_calls": ["hidden"]},
        }
        prompt = _context_prompt(state)  # type: ignore[arg-type]
        assert "2026-08-27" in prompt
        assert "below_reorder_point" in prompt
        assert "_planned_calls" not in prompt, "internal keys must not reach the model"

    def test_every_agent_has_a_real_operating_brief(self):
        for spec in all_agents().values():
            assert len(spec.system_prompt) > 200, f"{spec.name} has a thin prompt"
            assert "\n" in spec.system_prompt
