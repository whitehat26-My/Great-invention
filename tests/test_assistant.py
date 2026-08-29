"""The owner's question desk.

The property that matters most is that asking cannot become acting. The desk is
read-only by construction — no tools are bound to the model — and these tests
hold that line where a prompt-level promise would not.
"""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

from restaurant_ai.assistant import answer, build_snapshot, explain_model_failure
from restaurant_ai.config import reset_settings_cache

pytestmark = pytest.mark.db


class TestTheSnapshot:
    def test_it_carries_what_the_agents_themselves_see(self, db):
        snapshot = build_snapshot(db)
        assert set(snapshot) >= {
            "business_date",
            "today",
            "needs_your_approval",
            "what_each_agent_sees",
        }
        # Reported by person, the way the owner refers to them.
        seen = snapshot["what_each_agent_sees"]
        assert "Rain" in seen
        assert "Stock Tracking" in seen["Rain"]

    def test_it_reports_the_restaurants_real_numbers(self, db, stock_is_low):
        snapshot = build_snapshot(db)
        assert "below_reorder_point" in snapshot["what_each_agent_sees"]["Rain"]

    def test_a_broken_view_is_a_line_not_a_failure(self, db, monkeypatch):
        """An owner asking about the roster still gets it when stock is broken."""
        from restaurant_ai import brief as brief_module

        real = brief_module._perceive

        def selective(session, agent_name, business_date):
            if agent_name == "stock_reorder":
                raise RuntimeError("stock view is on fire")
            return real(session, agent_name, business_date)

        monkeypatch.setattr(brief_module, "_perceive", selective)
        snapshot = build_snapshot(db)

        assert "unavailable" in snapshot["what_each_agent_sees"]["Rain"]
        assert "stock view is on fire" in snapshot["what_each_agent_sees"]["Rain"]
        # Henry is untouched.
        assert "unavailable" not in snapshot["what_each_agent_sees"]["Henry"]

    def test_a_sweeping_view_is_capped_before_it_reaches_a_prompt(self, db, monkeypatch):
        """A one-line question must not cost a whole table of context."""
        from restaurant_ai import brief as brief_module

        monkeypatch.setattr(brief_module, "_perceive", lambda *a, **k: {"items": ["x" * 100] * 500})
        snapshot = build_snapshot(db)
        for view in snapshot["what_each_agent_sees"].values():
            assert len(view) < 3000
            assert "truncated" in view


class TestItCannotAct:
    def test_the_model_is_given_no_tools_at_all(self, db, monkeypatch):
        """Read-only by construction: nothing is bound, so nothing can be called.

        A prompt that says "do not change anything" is a request. This asserts
        the stronger property — the model has no way to act even if it tries.
        """
        captured = {}

        class Recorder:
            def bind_tools(self, *args, **kwargs):  # pragma: no cover - must not happen
                captured["bound"] = args
                raise AssertionError("the question desk must never bind tools")

            def invoke(self, messages):
                captured["messages"] = messages

                class Response:
                    content = "Rain does the ordering, and it needs your approval."

                return Response()

        monkeypatch.setattr("restaurant_ai.kernel.llm.is_fake", lambda interactive=False: False)
        monkeypatch.setattr("restaurant_ai.kernel.llm.get_model", lambda tier, **kw: Recorder())

        reply = answer("Order 50kg of prawns right now.", session=db)

        assert "bound" not in captured
        assert "Rain" in reply

    def test_the_brief_says_it_cannot_change_anything(self, db, monkeypatch):
        captured = {}

        class Recorder:
            def invoke(self, messages):
                captured["system"] = messages[0].content

                class Response:
                    content = "ok"

                return Response()

        monkeypatch.setattr("restaurant_ai.kernel.llm.is_fake", lambda interactive=False: False)
        monkeypatch.setattr("restaurant_ai.kernel.llm.get_model", lambda tier, **kw: Recorder())
        answer("anything", session=db)

        # The claim, not its wording: this must survive a rewrite of the prompt's
        # voice, which is the thing most likely to change about it.
        system = captured["system"].lower()
        assert "cannot change anything" in system
        assert "no tools" in system
        assert "never imply" in system

    def test_it_is_told_the_currency_and_timezone(self, db, monkeypatch):
        """The order agent once quoted a ringgit dish in dollars. Not again."""
        captured = {}

        class Recorder:
            def invoke(self, messages):
                captured["system"] = messages[0].content

                class Response:
                    content = "ok"

                return Response()

        monkeypatch.setattr("restaurant_ai.kernel.llm.is_fake", lambda interactive=False: False)
        monkeypatch.setattr("restaurant_ai.kernel.llm.get_model", lambda tier, **kw: Recorder())
        answer("what is the average check?", session=db)

        assert "RM" in captured["system"]
        assert "Asia/Kuala_Lumpur" in captured["system"]


class TestAnswering:
    def test_an_empty_question_invites_one(self, db):
        assert "Ask me anything" in answer("   ", session=db)

    def test_without_a_model_it_still_reports_real_numbers(self, db):
        """ "No model configured" must not mean "no answer"."""
        reply = answer("how are we doing?", session=db)
        assert "No language model is configured" in reply
        assert "money:" in reply

    def test_a_long_answer_is_cut_to_fit_a_phone(self, db, monkeypatch):
        class Recorder:
            def invoke(self, messages):
                class Response:
                    content = "x" * 5000

                return Response()

        monkeypatch.setattr("restaurant_ai.kernel.llm.is_fake", lambda interactive=False: False)
        monkeypatch.setattr("restaurant_ai.kernel.llm.get_model", lambda tier, **kw: Recorder())

        reply = answer("tell me everything", session=db)
        assert len(reply) <= 1201
        assert reply.endswith("…")

    def test_thinking_never_reaches_the_owner(self, db, monkeypatch):
        """Claude thinks in blocks; a raw str() would send the owner its reasoning."""

        class Recorder:
            def invoke(self, messages):
                class Response:
                    content = [
                        {"type": "thinking", "thinking": "the owner must not read this"},
                        {"type": "text", "text": "Seven ingredients are low."},
                    ]

                return Response()

        monkeypatch.setattr("restaurant_ai.kernel.llm.is_fake", lambda interactive=False: False)
        monkeypatch.setattr("restaurant_ai.kernel.llm.get_model", lambda tier, **kw: Recorder())

        reply = answer("what is low?", session=db)
        assert reply == "Seven ingredients are low."
        assert "must not read this" not in reply


class TestTheCli:
    def test_ask_answers_from_the_command_line(self, db):
        from restaurant_ai.cli import app

        result = CliRunner().invoke(app, ["ask", "how are we doing?"])
        assert result.exit_code == 0, result.output
        assert "money:" in result.output


class TestRoutingAnInstruction:
    """Routing decides whether words are a question or a job. Wrong is expensive."""

    def _model(self, monkeypatch, verdict: str):
        class Recorder:
            def invoke(self, messages):
                class Response:
                    content = verdict

                return Response()

        monkeypatch.setattr("restaurant_ai.kernel.llm.is_fake", lambda interactive=False: False)
        monkeypatch.setattr("restaurant_ai.kernel.llm.get_model", lambda tier, **kw: Recorder())

    def test_a_question_routes_to_answering(self, db, monkeypatch):
        self._model(monkeypatch, "QUESTION")
        from restaurant_ai.assistant import route

        assert route("how much chicken?").kind == "question"

    def test_an_instruction_routes_to_the_agent(self, db, monkeypatch):
        self._model(monkeypatch, "RUN stock_reorder")
        from restaurant_ai.assistant import route

        intent = route("restock the kitchen")
        assert intent.kind == "run"
        assert intent.agent == "stock_reorder"

    def test_a_person_name_routes_too(self, db, monkeypatch):
        """The model may answer with the name the owner uses."""
        self._model(monkeypatch, "RUN Rain")
        from restaurant_ai.assistant import route

        assert route("get Rain on the stock").agent == "stock_reorder"

    def test_a_misroute_to_nobody_becomes_a_question_to_the_owner(self, db, monkeypatch):
        """Answering RUN and then naming a stranger must not run anything."""
        self._model(monkeypatch, "RUN gordon_ramsay")
        from restaurant_ai.assistant import route

        intent = route("shout at the kitchen")
        assert intent.kind == "unclear"
        assert "gordon_ramsay" in intent.reason

    def test_an_unparseable_verdict_answers_rather_than_refusing(self, db, monkeypatch):
        """Changed deliberately: this used to refuse, and refusing was wrong.

        The two mistakes are not symmetric. Answering an instruction costs a
        reply naming whose job it is, which is useful. Running an agent nobody
        asked for is not recoverable by reading. So the safe guess is the one
        that only ever reads.
        """
        self._model(monkeypatch, "I think maybe you want to reorder something?")
        from restaurant_ai.assistant import route

        assert route("hmm").kind == "question"

    def test_a_model_that_fails_routes_to_unclear(self, db, monkeypatch):
        """Rate-limited or unreachable must not become a coin-flip."""

        class Broken:
            def invoke(self, messages):
                raise RuntimeError("429 quota exceeded")

        monkeypatch.setattr("restaurant_ai.kernel.llm.is_fake", lambda interactive=False: False)
        monkeypatch.setattr("restaurant_ai.kernel.llm.get_model", lambda tier, **kw: Broken())
        from restaurant_ai.assistant import route

        intent = route("restock the kitchen")
        assert intent.kind == "unclear"
        # The owner gets the plain-English version, not the provider's 429.
        assert "free quota" in intent.reason
        assert "429" not in intent.reason

    def test_nothing_said_is_unclear(self, db):
        from restaurant_ai.assistant import route

        assert route("   ").kind == "unclear"

    def test_the_router_is_told_every_agent_it_may_name(self, db, monkeypatch):
        captured = {}

        class Recorder:
            def invoke(self, messages):
                captured["system"] = messages[0].content

                class Response:
                    content = "QUESTION"

                return Response()

        monkeypatch.setattr("restaurant_ai.kernel.llm.is_fake", lambda interactive=False: False)
        monkeypatch.setattr("restaurant_ai.kernel.llm.get_model", lambda tier, **kw: Recorder())
        from restaurant_ai.assistant import route
        from restaurant_ai.kernel.registry import all_agents

        route("anything")
        for name in all_agents():
            assert name in captured["system"]
        assert "Never guess between two" in captured["system"]


class TestFindingAnAgentWithoutAModel:
    """The path that still works when the model does not."""

    def test_a_person_name_finds_the_agent(self):
        from restaurant_ai.assistant import find_agent

        assert find_agent("Rain") == "stock_reorder"
        assert find_agent("  rain  ") == "stock_reorder"

    def test_a_slug_finds_the_agent(self):
        from restaurant_ai.assistant import find_agent

        assert find_agent("stock_reorder") == "stock_reorder"
        assert find_agent("stock-reorder") == "stock_reorder"

    def test_a_stranger_finds_nobody(self):
        from restaurant_ai.assistant import find_agent

        assert find_agent("gordon") is None
        assert find_agent("") is None

    def test_without_a_model_naming_an_agent_still_runs_it(self, db):
        """No classifier, but "rain" is unambiguous without one."""
        from restaurant_ai.assistant import route

        assert route("rain").kind == "run"
        assert route("how are we doing?").kind == "question"


class TestWhenTheModelWillNotAnswer:
    """A person waiting on a chat cannot tell slow from dead. Neither may we."""

    def test_the_desk_uses_the_impatient_budget(self, db, monkeypatch):
        """Ten retries with no deadline is right for a nightly close, not a chat."""
        asked = {}

        class Recorder:
            def invoke(self, messages):
                class Response:
                    content = "ok"

                return Response()

        def spy(tier, *, interactive=False):
            asked["interactive"] = interactive
            return Recorder()

        monkeypatch.setattr("restaurant_ai.kernel.llm.is_fake", lambda interactive=False: False)
        monkeypatch.setattr("restaurant_ai.kernel.llm.get_model", spy)

        answer("how are we?", session=db)
        assert asked["interactive"] is True

    def test_routing_uses_it_too(self, db, monkeypatch):
        asked = {}

        class Recorder:
            def invoke(self, messages):
                class Response:
                    content = "QUESTION"

                return Response()

        def spy(tier, *, interactive=False):
            asked["interactive"] = interactive
            return Recorder()

        monkeypatch.setattr("restaurant_ai.kernel.llm.is_fake", lambda interactive=False: False)
        monkeypatch.setattr("restaurant_ai.kernel.llm.get_model", spy)

        from restaurant_ai.assistant import route

        route("anything")
        assert asked["interactive"] is True

    def test_an_exhausted_quota_is_answered_not_swallowed(self, db, monkeypatch):
        """The failure that actually happened: silence for minutes, then nothing."""

        class Exhausted:
            def invoke(self, messages):
                raise RuntimeError(
                    "Error calling model (RESOURCE_EXHAUSTED): 429 RESOURCE_EXHAUSTED. "
                    "Quota exceeded for generate_content_free_tier_requests, limit: 20"
                )

        monkeypatch.setattr("restaurant_ai.kernel.llm.is_fake", lambda interactive=False: False)
        monkeypatch.setattr("restaurant_ai.kernel.llm.get_model", lambda tier, **kw: Exhausted())

        reply = answer("how much stock?", session=db)
        assert "used up today's free quota" in reply
        # It must name what still works without a model.
        assert "/run" in reply and "/brief" in reply


class TestExplainingAModelFailure:
    """Three failures that actually happen, three different answers."""

    def test_a_quota_error_says_wait_and_what_still_works(self):
        from restaurant_ai.assistant import explain_model_failure

        said = explain_model_failure(RuntimeError("429 RESOURCE_EXHAUSTED quota exceeded"))
        assert "free quota" in said
        assert "counts per model" in said

    def test_a_timeout_says_try_again(self):
        from restaurant_ai.assistant import explain_model_failure

        assert "did not answer in time" in explain_model_failure(TimeoutError("deadline exceeded"))

    def test_a_bad_key_points_at_the_setting(self):
        from restaurant_ai.assistant import explain_model_failure

        said = explain_model_failure(RuntimeError("UNAUTHENTICATED: invalid api key"))
        assert "GOOGLE_API_KEY" in said
        assert "doctor" in said

    def test_anything_else_still_names_the_error(self):
        from restaurant_ai.assistant import explain_model_failure

        assert "ValueError" in explain_model_failure(ValueError("something odd"))


class TestNamesPeopleActuallyType:
    """`/run <Irma>` — the docs write `<name>`, and people paste the brackets."""

    def test_the_placeholder_brackets_are_not_taken_literally(self):
        from restaurant_ai.assistant import find_agent

        assert find_agent("<Irma>") == "menu_pricing"
        assert find_agent("<stock_reorder>") == "stock_reorder"

    def test_quotes_around_a_name_are_forgiven_too(self):
        from restaurant_ai.assistant import find_agent

        assert find_agent('"Rain"') == "stock_reorder"
        assert find_agent("'rain'") == "stock_reorder"

    def test_a_genuinely_unknown_name_is_still_unknown(self):
        """Forgiving punctuation must not become guessing at names."""
        from restaurant_ai.assistant import find_agent

        assert find_agent("<gordon>") is None
        assert find_agent("<>") is None


class TestTheVerdictAsModelsActuallyWriteIt:
    """The contract asks for one bare word. Models answer it in markdown."""

    def _model(self, monkeypatch, verdict: str):
        class Recorder:
            def invoke(self, messages):
                class Response:
                    content = verdict

                return Response()

        monkeypatch.setattr("restaurant_ai.kernel.llm.is_fake", lambda interactive=False: False)
        monkeypatch.setattr("restaurant_ai.kernel.llm.get_model", lambda tier, **kw: Recorder())

    @pytest.mark.parametrize(
        "dressed",
        [
            "**QUESTION**",
            "`QUESTION`",
            "QUESTION.",
            "\n\nQUESTION",
            "Answer: QUESTION",
            "> QUESTION",
            '"QUESTION"',
        ],
    )
    def test_a_question_is_recognised_however_it_is_dressed(self, db, monkeypatch, dressed):
        from restaurant_ai.assistant import route

        self._model(monkeypatch, dressed)
        assert route("how much stock?").kind == "question"

    @pytest.mark.parametrize("dressed", ["**RUN stock_reorder**", "Answer: RUN rain", "`RUN Rain`"])
    def test_an_instruction_is_too(self, db, monkeypatch, dressed):
        from restaurant_ai.assistant import route

        self._model(monkeypatch, dressed)
        intent = route("restock")
        assert intent.kind == "run"
        assert intent.agent == "stock_reorder"

    def test_an_unreadable_verdict_answers_rather_than_refusing(self, db, monkeypatch):
        """The two mistakes are not symmetric.

        Treating an instruction as a question costs a reply explaining whose
        job it is. Treating a question as an instruction runs an agent nobody
        asked for. So an unreadable verdict answers.
        """
        from restaurant_ai.assistant import route

        self._model(monkeypatch, "I think the owner is asking about stock levels")
        assert route("how are we doing?").kind == "question"

    def test_the_models_own_unclear_still_asks(self, db, monkeypatch):
        """Its judgement that it cannot tell is worth respecting."""
        from restaurant_ai.assistant import route

        self._model(monkeypatch, "UNCLEAR")
        intent = route("do the thing")
        assert intent.kind == "unclear"
        assert "question or a job" in intent.reason

    def test_run_naming_a_stranger_is_still_refused(self, db, monkeypatch):
        """Forgiving formatting must not become forgiving about which agent."""
        from restaurant_ai.assistant import route

        self._model(monkeypatch, "**RUN gordon_ramsay**")
        assert route("shout").kind == "unclear"


class TestGreetingsCostNothing:
    """Twenty model calls a day, and "hey" would spend two of them."""

    def test_a_greeting_never_reaches_the_model(self, db, monkeypatch):
        from restaurant_ai.assistant import route

        monkeypatch.setattr("restaurant_ai.kernel.llm.is_fake", lambda interactive=False: False)
        monkeypatch.setattr(
            "restaurant_ai.kernel.llm.get_model",
            lambda tier, **kw: pytest.fail("a greeting must not cost a model call"),
        )
        assert route("hey").kind == "greeting"

    @pytest.mark.parametrize(
        "said", ["hi", "Hello!", "  hey  ", "thanks", "terima kasih", "ok", "good morning"]
    )
    def test_the_ones_people_actually_send(self, db, said):
        from restaurant_ai.assistant import is_pleasantry

        assert is_pleasantry(said)

    @pytest.mark.parametrize(
        "said",
        ["how much stock?", "hey what are the numbers", "restock the kitchen", "okay run rain"],
    )
    def test_a_real_message_is_not_mistaken_for_one(self, db, said):
        """ "okay run rain" is an instruction that begins with a pleasantry."""
        from restaurant_ai.assistant import is_pleasantry

        assert not is_pleasantry(said)

    def test_the_greeting_says_what_can_be_done(self, db):
        from restaurant_ai.assistant import greet

        said = greet()
        assert "/agents" in said and "/help" in said


class TestATimeoutOnAFreeTierIsUsuallyTheQuota:
    def test_it_names_the_likely_cause_rather_than_blaming_the_network(self):
        from restaurant_ai.assistant import explain_model_failure

        said = explain_model_failure(TimeoutError("deadline exceeded"))
        assert "quota is spent" in said
        assert "doctor" in said
        # And what still works without any model at all.
        assert "/run" in said


class TestLocalModelFailures:
    """A machine under the counter fails differently from a hosted API.

    It shares vocabulary with the hosted failures without sharing their causes:
    "timed out" from a CPU grinding through an 8B model is not a spent quota,
    and telling the owner to wait until tomorrow for a quota they do not have
    sends them to fix the wrong thing.
    """

    @pytest.fixture
    def local(self, monkeypatch):
        monkeypatch.setenv("LLM_PROVIDER", "ollama")
        reset_settings_cache()
        yield
        reset_settings_cache()

    def test_ollama_not_running_says_so_and_says_why_the_window_matters(self, local):
        said = explain_model_failure(RuntimeError("connection refused to localhost:11434"))
        assert "not installed or not running" in said
        assert "opened before Ollama was installed" in said

    def test_a_model_that_was_never_pulled_gets_the_pull_command(self, local):
        said = explain_model_failure(RuntimeError('model "hermes3:8b" not found, try pulling it'))
        assert "ollama pull" in said

    def test_not_enough_memory_offers_a_smaller_model(self, local):
        said = explain_model_failure(RuntimeError("model requires more system memory"))
        assert "hermes3:3b" in said

    def test_a_slow_local_answer_is_not_reported_as_a_spent_quota(self, local):
        """The bug this exists for: there is no quota on this machine to spend."""
        said = explain_model_failure(TimeoutError("Request timed out"))
        assert "quota" not in said.lower()
        assert "normal rather than broken" in said
        assert "LLM_PROVIDER_INTERACTIVE" in said

    def test_hosted_quota_advice_is_untouched(self):
        """Every other deployment still gets the hosted explanation."""
        reset_settings_cache()
        said = explain_model_failure(RuntimeError("429 RESOURCE_EXHAUSTED"))
        assert "today's free quota" in said


class TestAMissingPackageIsNotAProviderFailure:
    """`git pull` brings code that needs a package; nothing installs it.

    It surfaces from the same call as a real provider error, so every other
    explanation here would have sent the owner to check a key, a network or a
    quota — none of which is involved. Adding any provider adds a dependency,
    so this outlives the one that revealed it.
    """

    def test_it_names_the_package_and_the_command(self):
        said = explain_model_failure(ModuleNotFoundError("No module named 'langchain_ollama'"))
        assert "pip install -e ." in said
        assert "not installed" in said

    def test_it_does_not_blame_the_network_or_the_key(self):
        said = explain_model_failure(ModuleNotFoundError("No module named 'langchain_ollama'"))
        assert "could not reach" not in said.lower()
        assert "quota" not in said.lower()

    def test_it_wins_over_the_local_model_explanations(self, monkeypatch):
        """A missing package under LLM_PROVIDER=ollama is still a missing package."""
        monkeypatch.setenv("LLM_PROVIDER", "ollama")
        reset_settings_cache()
        said = explain_model_failure(ModuleNotFoundError("No module named 'langchain_ollama'"))
        assert "pip install -e ." in said
        assert "not installed or not running" not in said
        reset_settings_cache()


class TestItFollowsAConversation:
    """The gap that made it a search box rather than a colleague.

    "how much chicken is left?" always worked. "and rice?" was unanswerable,
    because every message arrived with no idea that the first one had happened.
    """

    def _turns(self):
        from restaurant_ai.memory import KEANU, OWNER, Turn

        return [
            Turn(role=OWNER, text="how much chicken is left?"),
            Turn(role=KEANU, text="About 12kg — enough for tomorrow."),
        ]

    def test_the_exchange_reaches_the_model(self, db, monkeypatch):
        seen = {}

        class Model:
            def invoke(self, messages):
                seen["messages"] = messages

                class R:
                    content = "About 40kg of rice."

                return R()

        monkeypatch.setattr("restaurant_ai.kernel.llm.is_fake", lambda interactive=False: False)
        monkeypatch.setattr("restaurant_ai.kernel.llm.get_model", lambda tier, **kw: Model())

        answer("and rice?", session=db, history=self._turns())

        texts = [str(getattr(m, "content", "")) for m in seen["messages"]]
        assert any("how much chicken is left?" in t for t in texts), "the question is missing"
        assert any("About 12kg" in t for t in texts), "Keanu's own answer is missing"
        assert texts[-1] == "and rice?"

    def test_keanu_speaks_as_himself_not_as_the_owner(self, db, monkeypatch):
        """His turns must arrive as his, or the model reads its own words as
        instructions from the owner and answers them again."""
        from langchain_core.messages import AIMessage

        seen = {}

        class Model:
            def invoke(self, messages):
                seen["messages"] = messages

                class R:
                    content = "ok"

                return R()

        monkeypatch.setattr("restaurant_ai.kernel.llm.is_fake", lambda interactive=False: False)
        monkeypatch.setattr("restaurant_ai.kernel.llm.get_model", lambda tier, **kw: Model())

        answer("and rice?", session=db, history=self._turns())

        spoken = [m for m in seen["messages"] if isinstance(m, AIMessage)]
        assert [str(m.content) for m in spoken] == ["About 12kg — enough for tomorrow."]

    def test_no_history_still_answers(self, db, monkeypatch):
        """A first message has no thread, and must not be worse for it."""
        monkeypatch.setattr("restaurant_ai.kernel.llm.is_fake", lambda interactive=False: False)

        class Model:
            def invoke(self, messages):
                class R:
                    content = "About 40kg."

                return R()

        monkeypatch.setattr("restaurant_ai.kernel.llm.get_model", lambda tier, **kw: Model())

        assert answer("how much rice?", session=db) == "About 40kg."
