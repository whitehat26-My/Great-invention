"""The live-model path.

The whole platform runs on the deterministic path by default, which means the
LLM wiring could rot without any test noticing until someone sets a real key.
These exercise everything up to the network boundary: provider selection, model
construction, tool binding and the JSON schemas the model is shown — for both
live providers, because the two disagree about enough (sampling parameters,
thinking, how a nullable argument is spelled) that "it works on Claude" says
nothing about Gemini.

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


@pytest.fixture
def google_env(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "google")
    monkeypatch.setenv("GOOGLE_API_KEY", "not-a-real-key")
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

    @pytest.mark.parametrize("name", sorted(all_agents()))
    def test_every_tool_argument_is_described(self, name):
        """The model has nothing else to go on.

        An undescribed `party_size` is one the model has to infer from its name,
        and the difference between a party of two and a party of six is which
        table gets held.
        """
        undescribed = []
        for tool in get_agent(name).tools:
            schema = _as_langchain_tool(tool).args_schema.model_json_schema()
            for argument, meta in (schema.get("properties") or {}).items():
                # A $ref or allOf carries its description on the nested model.
                if not any(k in meta for k in ("description", "$ref", "allOf")):
                    undescribed.append(f"{tool.name}.{argument}")
        assert not undescribed, f"{name}: {', '.join(undescribed)}"


class TestGoogleProvider:
    """The second live provider. Everything above `llm.py` is provider-agnostic;
    what differs is confined here, and it differs in ways that are 400s rather
    than warnings."""

    def test_it_builds_the_configured_gemini_models(self, google_env):
        from langchain_google_genai import ChatGoogleGenerativeAI

        settings = get_settings()
        model = llm.get_model("reasoning")
        assert isinstance(model, ChatGoogleGenerativeAI)
        assert model.model.endswith(settings.google_model_reasoning)

    def test_a_missing_key_fails_loudly(self, monkeypatch):
        monkeypatch.setenv("LLM_PROVIDER", "google")
        monkeypatch.setenv("GOOGLE_API_KEY", "")
        reset_settings_cache()
        llm.reset_model_cache()
        with pytest.raises(RuntimeError, match="GOOGLE_API_KEY"):
            llm.get_model("reasoning")
        reset_settings_cache()

    def test_no_temperature_is_sent(self, google_env):
        """Gemini 3 Flash uses fixed sampling and discards one, warning per call.

        Same place Anthropic landed, for the same reason — and a warning on
        every call from every agent is its own kind of broken.
        """
        assert llm.get_model("reasoning").temperature is None

    def test_the_cache_does_not_confuse_providers(self, monkeypatch):
        # Keyed on the id alone, two providers configured with the same model
        # name hand back each other's client.
        monkeypatch.setenv("LLM_PROVIDER", "google")
        monkeypatch.setenv("GOOGLE_API_KEY", "k")
        monkeypatch.setenv("GOOGLE_MODEL_REASONING", "shared-name")
        monkeypatch.setenv("MODEL_REASONING", "shared-name")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
        reset_settings_cache()
        llm.reset_model_cache()
        google = llm.get_model("reasoning")

        monkeypatch.setenv("LLM_PROVIDER", "anthropic")
        reset_settings_cache()
        anthropic = llm.get_model("reasoning")
        assert type(google) is not type(anthropic)

    @pytest.mark.parametrize("name", sorted(all_agents()))
    def test_every_agents_tools_convert_for_gemini(self, google_env, name):
        """Gemini function declarations are an OpenAPI 3.0 subset.

        Nine arguments across five agents are `str | None`, which serialises as
        `anyOf: [string, null]` — historically the rough edge in this converter.
        """
        from langchain_google_genai._function_utils import (
            convert_to_genai_function_declarations,
        )

        spec = get_agent(name)
        tools = [_as_langchain_tool(t) for t in spec.tools]
        declared = convert_to_genai_function_declarations(tools)
        names = {f.name for tool in declared for f in (tool.function_declarations or [])}
        assert names == {t.name for t in spec.tools}

    def test_a_nullable_argument_survives_conversion(self, google_env):
        from langchain_google_genai._function_utils import (
            convert_to_genai_function_declarations,
        )

        tool = _as_langchain_tool(get_agent("bookkeeping").tool("reconcile_day"))
        declared = convert_to_genai_function_declarations([tool])
        properties = declared[0].function_declarations[0].parameters.properties
        # An optional argument has to keep both its nullability and the
        # description that says what belongs in it — losing either leaves the
        # model guessing at a field it was meant to be told about.
        assert properties["business_date"].nullable is True
        assert properties["business_date"].description


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


class TestTheExampleEnvFile:
    """`.env.example` is the documented way to start. It has to actually work."""

    def test_it_parses(self):
        from pathlib import Path

        from restaurant_ai.config import Settings

        example = Path(__file__).resolve().parents[1] / ".env.example"
        assert example.exists()
        # An empty `FOO=` means "not set". Left unhandled it is a validation
        # error that takes the platform down before it does anything.
        settings = Settings(_env_file=str(example))  # type: ignore[call-arg]
        assert settings.llm_provider == "fake"
        assert settings.google_reasoning_effort is None
        assert settings.llm_temperature is None

    def test_every_setting_it_names_exists(self):
        """A stale key in the example is a setting someone will set in vain."""
        from pathlib import Path

        from restaurant_ai.config import Settings

        example = Path(__file__).resolve().parents[1] / ".env.example"
        known = set(Settings.model_fields)
        unknown = [
            line.split("=", 1)[0].strip().lower()
            for line in example.read_text().splitlines()
            if "=" in line and not line.lstrip().startswith("#")
        ]
        assert [k for k in unknown if k not in known] == []


@pytest.fixture
def ollama_env(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "ollama")
    monkeypatch.setenv("OLLAMA_HOST", "http://localhost:11434")
    reset_settings_cache()
    llm.reset_model_cache()
    yield
    reset_settings_cache()
    llm.reset_model_cache()


class TestOllamaProvider:
    """A model on this machine: no key, no quota, no bill."""

    def test_it_builds_the_configured_local_models(self, ollama_env):
        from langchain_ollama import ChatOllama

        model = llm.get_model("conversational")
        assert isinstance(model, ChatOllama)
        assert model.model == get_settings().ollama_model_conversational

    def test_it_needs_no_api_key(self, ollama_env, monkeypatch):
        """The whole point. Every other provider raises without one."""
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
        reset_settings_cache()
        llm.reset_model_cache()
        assert llm.get_model("reasoning") is not None

    def test_the_context_window_is_set_explicitly(self, ollama_env):
        """Ollama's default is smaller than one agent's prompt, and it truncates
        the front of an over-long prompt without erroring — so the agent loses
        its instructions and reports confidently on nothing."""
        model = llm.get_model("reasoning")
        assert model.num_ctx == get_settings().ollama_context
        assert model.num_ctx >= 8192

    def test_every_agents_tools_bind(self, ollama_env):
        """A local model that cannot be given tools cannot run an agent at all."""
        for name in sorted(all_agents()):
            spec = get_agent(name)
            tools = [_as_langchain_tool(t) for t in spec.tools]
            if tools:
                assert llm.get_model(spec.model_tier).bind_tools(tools) is not None


class TestSplitProviders:
    """Who answers depends on whether anyone is waiting.

    A scheduled run at 06:00 can take three minutes on a machine under the
    counter and cost nothing. The same three minutes on the owner's question at
    lunchtime is a bot that looks dead. The two are different problems and this
    is the one setting that separates them.
    """

    @pytest.fixture
    def split(self, monkeypatch):
        monkeypatch.setenv("LLM_PROVIDER", "ollama")
        monkeypatch.setenv("LLM_PROVIDER_INTERACTIVE", "anthropic")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-not-a-real-key")
        reset_settings_cache()
        llm.reset_model_cache()
        yield
        reset_settings_cache()
        llm.reset_model_cache()

    def test_scheduled_work_goes_local(self, split):
        from langchain_ollama import ChatOllama

        assert isinstance(llm.get_model("reasoning"), ChatOllama)

    def test_the_owner_waiting_goes_to_the_cloud(self, split):
        from langchain_anthropic import ChatAnthropic

        assert isinstance(llm.get_model("conversational", interactive=True), ChatAnthropic)

    def test_the_two_do_not_share_a_cache_entry(self, split):
        """Same tier, different provider. A cache keyed on the model id alone
        would hand the chat whichever one was built first."""
        batch = llm.get_model("conversational")
        chat = llm.get_model("conversational", interactive=True)
        assert batch is not chat

    def test_unset_means_one_provider_for_everything(self, anthropic_env):
        """Every .env written before the split still means what it said."""
        assert get_settings().llm_provider_interactive is None
        assert llm.provider_for(interactive=True) == "anthropic"
        assert llm.provider_for(interactive=False) == "anthropic"

    def test_a_fake_batch_side_does_not_make_the_chat_fake(self, monkeypatch):
        """Running agents deterministically while the owner still gets answers."""
        monkeypatch.setenv("LLM_PROVIDER", "fake")
        monkeypatch.setenv("LLM_PROVIDER_INTERACTIVE", "anthropic")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-not-a-real-key")
        reset_settings_cache()
        llm.reset_model_cache()

        assert llm.is_fake() is True
        assert llm.is_fake(interactive=True) is False
        reset_settings_cache()
        llm.reset_model_cache()
