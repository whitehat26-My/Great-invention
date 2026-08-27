"""The two command-line surfaces the live run depends on.

`live-check` is what settles whether the credentials and the request shape work
before thirteen agents are set going, and `run-agent --path` is what pins one
agent to one planner so the model's choice can be held against what the
deterministic path would have done. Both are thin, and both would be found
broken at the worst possible moment.
"""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

from restaurant_ai.cli import app

pytestmark = pytest.mark.db

runner = CliRunner()


class TestLiveCheck:
    def test_it_refuses_under_the_fake_provider(self):
        # Rather than reporting a cheerful success having called nothing.
        result = runner.invoke(app, ["live-check"])
        assert result.exit_code == 1
        assert "LLM_PROVIDER=fake" in result.output

    def test_it_reports_the_configuration_either_way(self):
        result = runner.invoke(app, ["live-check"])
        assert "thinking" in result.output
        assert "max tokens" in result.output


class TestRunAgentPath:
    def test_an_unknown_path_is_rejected(self, db):
        result = runner.invoke(app, ["run-agent", "stock_reorder", "--path", "sideways"])
        assert result.exit_code == 1
        assert "--path must be" in result.output

    def test_the_deterministic_path_is_reported_as_such(self, db):
        result = runner.invoke(app, ["run-agent", "ordering", "--path", "deterministic"])
        assert result.exit_code == 0
        assert "path   deterministic" in result.output

    def test_forcing_the_model_without_one_fails_loudly(self, db):
        # A forced live run under LLM_PROVIDER=fake must not quietly fall back
        # to the deterministic path and report success.
        result = runner.invoke(app, ["run-agent", "ordering", "--path", "model"])
        assert result.exit_code == 1
        assert "status failed" in result.output

    def test_a_malformed_payload_does_not_reach_the_agent(self, db):
        result = runner.invoke(app, ["run-agent", "ordering", "--payload", "{not json"])
        assert result.exit_code != 0
