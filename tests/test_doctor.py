"""Why nothing happened.

The bug this exists for is silence: a message sent to the bot, and nothing back.
A working system and a dead one look identical from a phone, so every check here
has to turn one of those into a sentence.
"""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

from restaurant_ai.approvals.telegram import TelegramRejected, TelegramUnreachable
from restaurant_ai.config import reset_settings_cache
from restaurant_ai.doctor import Diagnosis, _check_listener, diagnose, render

pytestmark = pytest.mark.db


@pytest.fixture
def configured(monkeypatch, tmp_path):
    """A correctly configured machine: shell and .env saying the same thing.

    The chdir matters. Settings are read from the working directory, and the
    real project .env names a different bot — which is a genuine disagreement
    for doctor to report, and nothing to do with the test's subject.
    """
    # A correctly configured machine has a dashboard key too: without one the
    # dashboard and the system map refuse every request, which is not healthy.
    (tmp_path / ".env").write_text(
        "TELEGRAM_BOT_TOKEN=test-token\nTELEGRAM_CHAT_ID=998877\n"
        "APPROVAL_API_KEY=a-long-random-value\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "998877")
    monkeypatch.setenv("APPROVAL_API_KEY", "a-long-random-value")
    reset_settings_cache()
    yield
    reset_settings_cache()


def _bot(monkeypatch, *, webhook: str | None = None):
    monkeypatch.setattr(
        "restaurant_ai.approvals.telegram.describe_bot",
        lambda: {"username": "Keanu007_Bot", "name": "Keanu", "webhook_url": webhook},
    )
    monkeypatch.setattr(
        "restaurant_ai.approvals.telegram.describe_chat",
        lambda chat_id: {"id": chat_id, "type": "private", "name": "Sharif"},
    )


class TestIsAnythingListening:
    """The check that explains almost every quiet bot."""

    def test_a_conflict_means_a_listener_is_running(self):
        """Telegram allows one getUpdates at a time — the refusal is the proof."""

        def conflicted(method, **kw):
            raise TelegramRejected(
                "Telegram refused getUpdates: Conflict: terminated by other getUpdates request"
            )

        report = Diagnosis()
        _check_listener(report, conflicted)

        assert report.checks[0].ok
        assert "running" in report.checks[0].detail

    def test_an_answer_means_nobody_is_reading(self):
        report = Diagnosis()
        _check_listener(report, lambda method, **kw: {"result": []})

        assert not report.checks[0].ok
        assert "NOT RUNNING" in report.checks[0].detail
        # `up` first: the listener alone answers messages while beat never
        # fires, so the nightly close and the brief silently do not happen.
        assert "restaurant-ai up" in report.checks[0].fix
        assert report.checks[0].fix.index("restaurant-ai up") < report.checks[0].fix.index(
            "telegram-listen"
        )

    def test_waiting_messages_are_counted_and_named(self):
        """ "I sent commands and nothing happened" — this is that, in one line."""
        report = Diagnosis()
        _check_listener(report, lambda method, **kw: {"result": [{}, {}, {}]})

        assert "3 message(s) are waiting unanswered" in report.checks[0].detail

    def test_it_consumes_nothing(self):
        """A diagnosis that ate the backlog would destroy what it reports on."""
        seen = {}

        def spy(method, **kw):
            seen.update(kw)
            return {"result": []}

        _check_listener(Diagnosis(), spy)
        assert "offset" not in seen

    def test_an_unreachable_telegram_is_not_reported_as_a_dead_listener(self):
        report = Diagnosis()
        _check_listener(
            report,
            lambda method, **kw: (_ for _ in ()).throw(TelegramUnreachable("proxy said no")),
        )
        assert not report.checks[0].ok
        assert "could not check" in report.checks[0].detail


class TestTheChain:
    def test_a_healthy_system_says_so(self, db, configured, monkeypatch):
        _bot(monkeypatch)
        monkeypatch.setattr(
            "restaurant_ai.approvals.telegram.api",
            lambda method, **kw: (_ for _ in ()).throw(
                TelegramRejected("Conflict: terminated by other getUpdates request")
            ),
        )
        report = diagnose()
        assert report.healthy, render(report)
        assert "Everything is up" in render(report)

    def test_a_missing_token_is_named_with_its_fix(self, db, monkeypatch):
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "")
        reset_settings_cache()
        report = diagnose()
        telegram = next(c for c in report.checks if c.name == "telegram bot")
        assert not telegram.ok
        assert "BotFather" in telegram.fix
        reset_settings_cache()

    def test_a_network_block_is_not_reported_as_a_bad_token(self, db, configured, monkeypatch):
        """The two look identical and the fixes are nothing alike."""
        monkeypatch.setattr(
            "restaurant_ai.approvals.telegram.describe_bot",
            lambda: (_ for _ in ()).throw(TelegramUnreachable("the proxy refused")),
        )
        report = diagnose()
        telegram = next(c for c in report.checks if c.name == "telegram bot")
        assert "network, not the token" in telegram.fix

    def test_a_revoked_token_is_reported_as_a_token_problem(self, db, configured, monkeypatch):
        """A well-formed token that Telegram refuses: nothing to see in the shape,
        so the advice is the one that applies — fetch another."""
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "8032262694:" + "A" * 35)
        reset_settings_cache()
        monkeypatch.setattr(
            "restaurant_ai.approvals.telegram.describe_bot",
            lambda: (_ for _ in ()).throw(TelegramRejected("Unauthorized")),
        )
        report = diagnose()
        telegram = next(c for c in report.checks if c.name == "telegram bot")
        assert "revoked" in telegram.fix
        reset_settings_cache()

    def test_a_registered_webhook_is_flagged_because_polling_refuses(
        self, db, configured, monkeypatch
    ):
        _bot(monkeypatch, webhook="https://example.test/hook")
        monkeypatch.setattr("restaurant_ai.approvals.telegram.api", lambda m, **kw: {"result": []})
        report = diagnose()
        webhook = next(c for c in report.checks if c.name == "webhook")
        assert not webhook.ok
        assert "example.test" in webhook.detail

    def test_a_wrong_chat_id_explains_the_silence_it_causes(self, db, configured, monkeypatch):
        """A bad id is not an error anywhere — it is everyone being ignored."""
        _bot(monkeypatch)
        monkeypatch.setattr(
            "restaurant_ai.approvals.telegram.describe_chat",
            lambda chat_id: (_ for _ in ()).throw(TelegramRejected("chat not found")),
        )
        monkeypatch.setattr("restaurant_ai.approvals.telegram.api", lambda m, **kw: {"result": []})
        report = diagnose()
        chat = next(c for c in report.checks if c.name == "approvals chat")
        assert not chat.ok
        assert "ignored in silence" in chat.fix

    def test_the_settings_file_is_checked_before_the_database(self, db):
        """Which .env was read decides *which* database the next check reports on.

        The database used to be first, on the grounds that every other answer
        depends on it. That is still true of every answer except this one: run
        from the wrong folder, .env is not found, and a green database line is
        a green line about a database nobody configured.
        """
        names = [c.name for c in diagnose().checks]
        assert names[0] == "settings file"
        assert names.index("settings file") < names.index("database")


class TestTheCli:
    def test_doctor_exits_nonzero_when_something_is_broken(self, db, monkeypatch):
        from restaurant_ai.cli import app

        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "")
        reset_settings_cache()
        result = CliRunner().invoke(app, ["doctor"])
        assert result.exit_code == 1
        assert "TELEGRAM_BOT_TOKEN is not set" in result.output
        reset_settings_cache()

    def test_it_changes_nothing(self, db, monkeypatch):
        """Safe to run at any time, including mid-service."""
        from restaurant_ai.cli import app

        monkeypatch.setattr(
            "restaurant_ai.approvals.telegram.api",
            lambda method, **kw: (
                pytest.fail(f"doctor must not call {method} unchecked")
                if method not in ("getMe", "getWebhookInfo", "getChat", "getUpdates")
                else {"ok": True, "result": []}
            ),
        )
        CliRunner().invoke(app, ["doctor"])


class TestKnowingWhatIsRunning:
    """ "No such command" is almost never a missing command."""

    def test_version_names_the_code_it_actually_loaded(self):
        from restaurant_ai.cli import app

        result = CliRunner().invoke(app, ["--version"])
        assert result.exit_code == 0
        assert "restaurant-ai 0.1.0" in result.output
        # The path is the whole point: a pulled checkout and a stale install
        # differ here and nowhere else.
        assert "running from" in result.output
        assert "restaurant_ai" in result.output

    def test_it_reports_the_commit_so_today_s_code_is_recognisable(self):
        """The version string never moves between releases. The commit does."""
        from restaurant_ai.cli import app

        result = CliRunner().invoke(app, ["--version"])
        assert "commit" in result.output

    def test_an_installed_copy_says_a_pull_will_not_help(self, monkeypatch, tmp_path):
        """The actual failure: checkout pulled, install not — so a pull is a no-op."""
        import subprocess

        from restaurant_ai import cli

        monkeypatch.setattr(cli, "__file__", str(tmp_path / "cli.py"))
        real = subprocess.run

        def not_a_repo(*args, **kwargs):
            if args and args[0][:2] == ["git", "rev-parse"]:
                return subprocess.CompletedProcess(args[0], 128, "", "not a git repository")
            return real(*args, **kwargs)

        monkeypatch.setattr(subprocess, "run", not_a_repo)
        described = cli._describe_install()
        assert "installed copy, not a checkout" in described
        assert "pip install -e ." in described


class TestConfiguredIsNotWorking:
    """A right key and a spent free tier look identical from the settings."""

    @pytest.fixture(autouse=True)
    def live_provider(self, monkeypatch):
        """Configure a provider the way a deployment does.

        These used to stub `describe_provider`, which stopped being enough when
        the check started resolving the provider per call — the settings said
        fake, so it reported fake and never rang the model that was stubbed to
        fail. Setting the real knob keeps the test honest about the mechanism.
        """
        monkeypatch.setenv("LLM_PROVIDER", "google")
        monkeypatch.setenv("GOOGLE_API_KEY", "not-a-real-key")
        monkeypatch.delenv("LLM_PROVIDER_INTERACTIVE", raising=False)
        reset_settings_cache()
        yield
        reset_settings_cache()

    def test_the_model_is_actually_called(self, db, monkeypatch):
        called = {}

        class Recorder:
            def invoke(self, messages):
                called["asked"] = True

                class Response:
                    content = "ok"

                return Response()

        monkeypatch.setattr(
            "restaurant_ai.kernel.llm.describe_provider",
            lambda: {"provider": "google", "conversational": "gemini-3.5-flash"},
        )
        monkeypatch.setattr("restaurant_ai.kernel.llm.get_model", lambda tier, **kw: Recorder())
        report = diagnose()

        assert called.get("asked"), "doctor must ask the model, not just read settings"
        model = next(c for c in report.checks if c.name == "language model")
        assert model.ok and "answering" in model.detail

    def test_an_exhausted_quota_fails_the_check_with_its_fix(self, db, monkeypatch):
        monkeypatch.setattr(
            "restaurant_ai.kernel.llm.describe_provider",
            lambda: {"provider": "google", "conversational": "gemini-3.5-flash"},
        )
        monkeypatch.setattr(
            "restaurant_ai.kernel.llm.get_model",
            lambda tier, **kw: (_ for _ in ()).throw(RuntimeError("429 RESOURCE_EXHAUSTED")),
        )
        report = diagnose()
        model = next(c for c in report.checks if c.name == "language model")
        assert not model.ok
        assert "free quota" in model.fix

    def test_a_page_of_provider_json_is_trimmed_to_one_line(self, db, monkeypatch):
        monkeypatch.setattr(
            "restaurant_ai.kernel.llm.describe_provider",
            lambda: {"provider": "google", "conversational": "gemini-3.5-flash"},
        )
        monkeypatch.setattr(
            "restaurant_ai.kernel.llm.get_model",
            lambda tier, **kw: (_ for _ in ()).throw(RuntimeError("x" * 4000)),
        )
        for line in render(diagnose()).splitlines():
            assert len(line) < 250


class TestConfiguration:
    """The two ways .env silently does nothing."""

    def _run(self) -> Diagnosis:
        from restaurant_ai.doctor import _check_configuration

        report = Diagnosis()
        _check_configuration(report)
        return report

    def _check(self, report: Diagnosis, name: str):
        return next((c for c in report.checks if c.name == name), None)

    def test_it_names_the_env_file_it_actually_read(self, tmp_path, monkeypatch):
        """Relative to the working directory, which is the whole trap."""
        (tmp_path / ".env").write_text("LLM_PROVIDER=fake\n")
        monkeypatch.chdir(tmp_path)
        reset_settings_cache()

        check = self._check(self._run(), "settings file")

        assert check is not None and check.ok
        assert str(tmp_path) in check.detail

    def test_an_env_txt_is_named_because_explorer_hides_it(self, tmp_path, monkeypatch):
        """Notepad's "save as .env" produces .env.txt, and Windows hides the .txt."""
        (tmp_path / ".env.txt").write_text("LLM_PROVIDER=anthropic\n")
        monkeypatch.chdir(tmp_path)
        reset_settings_cache()

        check = self._check(self._run(), "settings file")

        assert check is not None and not check.ok
        assert ".env.txt" in check.detail
        assert "Rename-Item" in check.fix

    def test_the_example_file_is_not_mistaken_for_a_near_miss(self, tmp_path, monkeypatch):
        """Every checkout has .env.example; naming it as a candidate is noise."""
        (tmp_path / ".env.example").write_text("LLM_PROVIDER=fake\n")
        monkeypatch.chdir(tmp_path)
        reset_settings_cache()

        check = self._check(self._run(), "settings file")

        assert check is not None and "found" not in check.detail

    def test_a_shell_variable_disagreeing_with_the_file_is_reported(self, tmp_path, monkeypatch):
        """The file is read, then overridden. Editing it can never help."""
        (tmp_path / ".env").write_text("LLM_PROVIDER=anthropic\n")
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("LLM_PROVIDER", "google")
        reset_settings_cache()

        check = self._check(self._run(), "environment")

        assert check is not None and not check.ok
        assert "LLM_PROVIDER=google" in check.detail
        assert "the file says anthropic" in check.detail
        reset_settings_cache()

    def test_an_agreeing_environment_is_not_a_fault(self, tmp_path, monkeypatch):
        """Compose passes every setting as an environment variable. That is normal."""
        (tmp_path / ".env").write_text("LLM_PROVIDER=anthropic\n")
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("LLM_PROVIDER", "anthropic")
        reset_settings_cache()

        assert self._check(self._run(), "environment") is None
        reset_settings_cache()

    def test_a_disagreeing_secret_is_named_without_printing_it(self, tmp_path, monkeypatch):
        """doctor output gets pasted into chats. It must not carry the key with it."""
        (tmp_path / ".env").write_text("ANTHROPIC_API_KEY=sk-ant-from-the-file\n")
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-from-the-shell")
        reset_settings_cache()

        check = self._check(self._run(), "environment")

        assert check is not None and not check.ok
        assert "ANTHROPIC_API_KEY" in check.detail
        assert "sk-ant-from-the-shell" not in check.detail
        assert "sk-ant-from-the-file" not in check.detail
        reset_settings_cache()

    def test_a_missing_env_says_every_setting_is_defaulted(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        reset_settings_cache()

        check = self._check(self._run(), "settings file")

        assert check is not None and not check.ok
        assert "default" in check.detail
        assert "folder you run them in" in check.fix

    def test_a_key_for_the_wrong_provider_is_flagged(self, tmp_path, monkeypatch):
        """Half a provider switch looks exactly like a finished one."""
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("LLM_PROVIDER", "google")
        monkeypatch.setenv("GOOGLE_API_KEY", "g")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-whatever")
        reset_settings_cache()

        check = self._check(self._run(), "provider")

        assert check is not None and not check.ok
        assert "anthropic" in check.detail
        assert "LLM_PROVIDER=anthropic" in check.fix
        reset_settings_cache()

    def test_a_matching_key_is_not_flagged(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("LLM_PROVIDER", "anthropic")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-whatever")
        monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
        reset_settings_cache()

        assert self._check(self._run(), "provider") is None
        reset_settings_cache()


class TestASplitDeploymentIsCheckedOnBothSides:
    """Four fifths of the calls go to the scheduled provider, and its failure is
    the silent one: the chat keeps answering while every agent stops thinking."""

    @pytest.fixture
    def split(self, monkeypatch, tmp_path):
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("LLM_PROVIDER", "ollama")
        monkeypatch.setenv("LLM_PROVIDER_INTERACTIVE", "anthropic")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-not-a-real-key")
        reset_settings_cache()
        yield
        reset_settings_cache()

    def test_both_providers_are_called(self, split, monkeypatch):
        from restaurant_ai.doctor import _check_model
        from restaurant_ai.kernel import llm

        asked: list[bool] = []

        class Answering:
            def invoke(self, messages):
                return type("R", (), {"content": "ok"})()

        def spy(tier="conversational", *, interactive=False):
            asked.append(interactive)
            return Answering()

        monkeypatch.setattr(llm, "get_model", spy)
        report = Diagnosis()
        _check_model(report)

        assert sorted(asked) == [False, True], "both sides must actually be called"
        assert [c.name for c in report.checks] == [
            "language model (agents)",
            "language model (chat)",
        ]
        assert all(c.ok for c in report.checks)

    def test_a_dead_local_model_fails_even_though_the_chat_works(self, split, monkeypatch):
        """The exact shape of the silent failure."""
        from restaurant_ai.doctor import _check_model
        from restaurant_ai.kernel import llm

        class Answering:
            def invoke(self, messages):
                return type("R", (), {"content": "ok"})()

        class Dead:
            def invoke(self, messages):
                raise RuntimeError("connection refused to localhost:11434")

        monkeypatch.setattr(
            llm,
            "get_model",
            lambda tier="conversational", *, interactive=False: (
                Answering() if interactive else Dead()
            ),
        )
        report = Diagnosis()
        _check_model(report)

        agents = next(c for c in report.checks if c.name == "language model (agents)")
        chat = next(c for c in report.checks if c.name == "language model (chat)")
        assert not agents.ok
        assert chat.ok
        assert "not installed or not running" in agents.fix

    def test_one_provider_still_reports_one_line(self, db, monkeypatch, tmp_path):
        """No new noise on every machine that has not split anything."""
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("LLM_PROVIDER", "fake")
        monkeypatch.delenv("LLM_PROVIDER_INTERACTIVE", raising=False)
        reset_settings_cache()

        report = Diagnosis()
        from restaurant_ai.doctor import _check_model

        _check_model(report)
        assert [c.name for c in report.checks] == ["language model"]
        reset_settings_cache()


class TestTheDashboardWouldServe:
    """Unset, the key makes every dashboard page refuse — correctly, and in
    silence. The pages do not load and every other check is green."""

    def _dashboard(self, monkeypatch, key: str):
        from restaurant_ai.doctor import _check_dashboard

        monkeypatch.setenv("APPROVAL_API_KEY", key)
        reset_settings_cache()
        report = Diagnosis()
        _check_dashboard(report)
        reset_settings_cache()
        return report.checks[0]

    def test_a_missing_key_is_reported_as_a_closed_dashboard(self, monkeypatch):
        check = self._dashboard(monkeypatch, "")

        assert not check.ok
        assert "refuse to serve" in check.detail
        assert "APPROVAL_API_KEY" in check.fix

    def test_a_key_that_is_set_passes(self, monkeypatch):
        assert self._dashboard(monkeypatch, "a-long-random-value").ok

    def test_the_key_is_never_printed(self, monkeypatch):
        """doctor output gets pasted. This one guards the restaurant's numbers."""
        check = self._dashboard(monkeypatch, "sekret-value-nobody-should-see")

        assert "sekret-value-nobody-should-see" not in check.detail
        assert "sekret-value-nobody-should-see" not in check.fix


class TestWhyTheTokenCannotBeRight:
    """ "Wrong or revoked" is true of every rejection and useful for almost none.

    A token copied out of a chat message arrives with the sentence's full stops
    attached, or a space, or half of it. Each has a different fix from "get a new
    one from BotFather" — which is the advice that sends the owner back round the
    loop they have just finished.
    """

    def _shape(self, token: str) -> str:
        from restaurant_ai.doctor import _token_shape

        return _token_shape(token)

    GOOD = "8032262694:" + "A" * 35

    def test_a_real_token_has_nothing_to_report(self):
        assert self._shape(self.GOOD) == ""

    def test_trailing_dots_from_a_sentence(self):
        """The exact way a token pasted into chat arrives."""
        said = self._shape(self.GOOD + "....")
        assert "full stop" in said

    def test_surrounding_whitespace(self):
        assert "space or newline" in self._shape(f" {self.GOOD}\n")

    def test_a_fragment_with_no_colon(self):
        assert "no colon" in self._shape("8032262694AAFBIEAPIxgoFEeGVvWq4")

    def test_a_secret_of_the_wrong_length_says_the_length(self):
        said = self._shape("8032262694:" + "A" * 20)
        assert "20 characters" in said
        assert "35" in said

    def test_characters_a_token_never_contains(self):
        assert "never contains" in self._shape("8032262694:" + "A" * 34 + "!")

    def test_a_bot_id_that_is_not_a_number(self):
        assert "not a number" in self._shape("notanid:" + "A" * 35)

    def test_it_never_returns_the_token(self):
        """This becomes a doctor line, and doctor output gets pasted."""
        token = self.GOOD + "...."
        assert "AAAA" not in self._shape(token)

    def test_the_check_prefers_the_shape_to_the_stock_advice(self, db, configured, monkeypatch):
        from restaurant_ai.approvals.telegram import TelegramRejected

        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", self.GOOD + "....")
        reset_settings_cache()
        monkeypatch.setattr(
            "restaurant_ai.approvals.telegram.describe_bot",
            lambda: (_ for _ in ()).throw(TelegramRejected("Unauthorized")),
        )
        report = diagnose()
        bot = next(c for c in report.checks if c.name == "telegram bot")

        assert "full stop" in bot.fix
        assert "Get a fresh one" not in bot.fix
        reset_settings_cache()
