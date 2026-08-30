"""What `up` does about a database that is not there.

The failure that shaped this came off a real Windows laptop: "Postgres is not
reachable... start it first" — advice that was already tried and answered with
"how?". The three reasons it can be missing (no Docker, Docker asleep, the
containers failed) have three different fixes, and the point of these tests is
that each situation gets its own sentence and none gets someone else's.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from restaurant_ai import services
from restaurant_ai.config import reset_settings_cache


class TestEnsureDatabase:
    def test_a_reachable_database_starts_nothing(self, monkeypatch):
        monkeypatch.setattr(services, "database_error", lambda: None)
        monkeypatch.setattr(
            services,
            "start_data_services",
            lambda folder: pytest.fail("must not touch docker when Postgres answers"),
        )
        ok, message = services.ensure_database()
        assert ok
        assert "up" in message

    def test_no_docker_says_install_it_not_start_it(self, monkeypatch):
        monkeypatch.setattr(services, "database_error", lambda: "OperationalError: refused")
        monkeypatch.setattr(services, "docker_state", lambda: "absent")
        ok, message = services.ensure_database()
        assert not ok
        assert "Docker is not installed" in message
        assert "docker.com" in message

    def test_a_sleeping_docker_desktop_is_its_own_situation(self, monkeypatch):
        """On Windows this is the common one: installed, but nobody opened it."""
        monkeypatch.setattr(services, "database_error", lambda: "timeout expired")
        monkeypatch.setattr(services, "docker_state", lambda: "engine-down")
        ok, message = services.ensure_database()
        assert not ok
        assert "installed but not running" in message
        assert "Docker Desktop" in message

    def test_with_docker_awake_it_starts_the_services_itself(self, monkeypatch, tmp_path):
        """The whole point: starting Postgres is our job when it can be."""
        (tmp_path / "docker-compose.yml").write_text("services: {}")
        monkeypatch.setattr(services, "database_error", lambda: "refused")
        monkeypatch.setattr(services, "docker_state", lambda: "ready")
        monkeypatch.setattr(services, "compose_dir", lambda start=None: tmp_path)

        started = {}

        def fake_start(folder):
            started["in"] = folder
            return True

        monkeypatch.setattr(services, "start_data_services", fake_start)
        monkeypatch.setattr(services, "wait_for_database", lambda: None)

        ok, message = services.ensure_database()
        assert ok
        assert started["in"] == tmp_path
        assert "Started Postgres and Redis" in message

    def test_a_start_that_fails_points_at_dockers_own_output(self, monkeypatch, tmp_path):
        monkeypatch.setattr(services, "database_error", lambda: "refused")
        monkeypatch.setattr(services, "docker_state", lambda: "ready")
        monkeypatch.setattr(services, "compose_dir", lambda start=None: tmp_path)
        monkeypatch.setattr(services, "start_data_services", lambda folder: False)
        ok, message = services.ensure_database()
        assert not ok
        assert "output above" in message

    def test_containers_up_but_postgres_silent_names_the_log_to_read(self, monkeypatch, tmp_path):
        monkeypatch.setattr(services, "database_error", lambda: "refused")
        monkeypatch.setattr(services, "docker_state", lambda: "ready")
        monkeypatch.setattr(services, "compose_dir", lambda start=None: tmp_path)
        monkeypatch.setattr(services, "start_data_services", lambda folder: True)
        monkeypatch.setattr(services, "wait_for_database", lambda: "still refused")
        ok, message = services.ensure_database()
        assert not ok
        assert "docker compose logs postgres" in message

    def test_outside_the_project_it_says_where_to_stand(self, monkeypatch):
        monkeypatch.setattr(services, "database_error", lambda: "refused")
        monkeypatch.setattr(services, "docker_state", lambda: "ready")
        monkeypatch.setattr(services, "compose_dir", lambda start=None: None)
        ok, message = services.ensure_database()
        assert not ok
        assert "Great-invention folder" in message


class TestFindingTheComposeFile:
    def test_found_here_or_above_but_never_invented(self, tmp_path):
        project = tmp_path / "repo"
        nested = project / "src" / "deep"
        nested.mkdir(parents=True)
        (project / "docker-compose.yml").write_text("services: {}")

        assert services.compose_dir(nested) == project
        assert services.compose_dir(tmp_path) is None


class TestTheStartupLauncher:
    """Startup folder, not schtasks: the real machine answered Access is denied."""

    def test_the_launcher_carries_the_project_and_this_interpreter(self, tmp_path):
        import sys

        script = services.write_startup_script(Path("C:/Users/User/Great-invention"), tmp_path)
        body = script.read_text()

        # cd first: .env is read from the working directory.
        assert 'cd /d "C:' in body
        assert body.index("cd /d") < body.index("-m restaurant_ai.cli up")
        assert sys.executable in body
        # A launcher that dies must leave its reason on screen.
        assert body.rstrip().endswith("pause")

    def test_remove_takes_it_out_and_says_when_there_was_nothing(self, tmp_path):
        services.write_startup_script(Path("/somewhere"), tmp_path)
        assert services.remove_startup_script(tmp_path) is True
        assert services.remove_startup_script(tmp_path) is False

    def test_no_appdata_means_no_folder_not_a_crash(self, monkeypatch):
        monkeypatch.delenv("APPDATA", raising=False)
        assert services.startup_folder() is None

    def test_the_cli_refuses_off_windows_and_names_the_alternative(self):
        """This sandbox is Linux, which is exactly the machine that must refuse."""
        from restaurant_ai.cli import app

        result = CliRunner().invoke(app, ["install-startup"])
        assert result.exit_code == 1
        assert "not Windows" in result.output
        assert "docker compose" in result.output


class TestTheMigrateCommand:
    """It existed everywhere except as something a person could type.

    `up` migrates on the way in and compose has a migrate service, so the gap
    only showed at the moment it cost most: a pull brings a new table, the owner
    reaches for the obvious command, and it is not there.
    """

    def test_it_exists_and_reports_what_it_did(self, db, monkeypatch):
        from typer.testing import CliRunner

        from restaurant_ai.cli import app

        monkeypatch.setattr(
            "restaurant_ai.services.migrate_database", lambda: (True, "Schema is up to date.")
        )
        result = CliRunner().invoke(app, ["migrate"])

        assert result.exit_code == 0
        assert "up to date" in result.output

    def test_a_failure_exits_nonzero(self, monkeypatch):
        """So a deploy script stops rather than starting against a stale schema."""
        from typer.testing import CliRunner

        from restaurant_ai.cli import app

        monkeypatch.setattr(
            "restaurant_ai.services.migrate_database",
            lambda: (False, "Could not apply migrations: connection refused"),
        )
        result = CliRunner().invoke(app, ["migrate"])

        assert result.exit_code == 1
        assert "connection refused" in result.output

    def test_reset_db_points_at_a_command_that_exists(self):
        """It used to name `make migrate`, which is not what a Windows owner has."""
        import inspect

        from restaurant_ai import cli

        source = inspect.getsource(cli.reset_db)
        assert "make migrate" not in source
        assert "restaurant-ai migrate" in source


class TestStartRealBringsItsOwnDatabase:
    """It was refusing with "run `restaurant-ai up`", which is worse than
    unhelpful: `up` starts the whole restaurant in a window the owner would
    then have to kill, and those four processes are exactly what must not be
    running while the schema is dropped underneath them."""

    def test_it_starts_the_database_before_dropping_anything(self, monkeypatch):
        from typer.testing import CliRunner

        from restaurant_ai.cli import app

        order = []
        monkeypatch.setattr(
            "restaurant_ai.services.ensure_database",
            lambda: (order.append("ensure"), (True, "Postgres is up."))[1],
        )
        monkeypatch.setattr(
            "restaurant_ai.db.base.get_engine",
            lambda: (_ for _ in ()).throw(AssertionError("dropped before ensuring")),
        )
        result = CliRunner().invoke(app, ["start-real", "--yes"])

        assert order == ["ensure"], "the database must be started first"
        assert "dropped before ensuring" not in result.output

    def test_a_database_it_cannot_start_stops_it(self, monkeypatch):
        from typer.testing import CliRunner

        from restaurant_ai.cli import app

        monkeypatch.setattr(
            "restaurant_ai.services.ensure_database",
            lambda: (False, "Docker Desktop is installed but not running."),
        )
        result = CliRunner().invoke(app, ["start-real", "--yes"])

        assert result.exit_code == 1
        assert "not running" in result.output

    def test_it_refuses_without_yes(self):
        from typer.testing import CliRunner

        from restaurant_ai.cli import app

        result = CliRunner().invoke(app, ["start-real"])
        assert result.exit_code == 1
        assert "erases every order" in result.output


class TestNoCommandGoesSilentForMinutes:
    """`ensure_database` can run `docker compose up`, pull images and then wait
    two minutes for Postgres to answer — and every word about that arrived at
    the end. A command that prints nothing for two minutes has not started, as
    far as anyone watching it is concerned, and the reasonable thing to do with
    it is press Ctrl-C."""

    def _first_line_before(self, monkeypatch, command: list[str]) -> str:
        from typer.testing import CliRunner

        from restaurant_ai.cli import app

        said: list[str] = []

        def slow() -> tuple[bool, str]:
            # Whatever was printed before this ran is all the owner sees while
            # Docker pulls images.
            said.append("|MARK|")
            return False, "Docker is installed but not running."

        monkeypatch.setattr("restaurant_ai.services.ensure_database", slow)
        result = CliRunner().invoke(app, command)
        return result.output.split("Docker is installed")[0]

    def test_start_real_says_what_it_is_doing_first(self, monkeypatch):
        before = self._first_line_before(monkeypatch, ["start-real", "--yes"])
        assert "Checking the database" in before
        assert "can take a minute" in before

    def test_up_says_it_too(self, monkeypatch):
        before = self._first_line_before(monkeypatch, ["up"])
        assert "Checking the database" in before


class TestFindingTheDashboard:
    """The dashboard refuses anyone without the key, and a browser address bar
    cannot send a header — so the key travels in the URL, and the owner was left
    to assemble that URL by hand out of a secret in a file. Opening
    localhost:8000/dashboard and being refused looks exactly like a dashboard
    that is broken, which is how a working page becomes one nobody visits."""

    def _run(self, monkeypatch, *, key: str, listening: bool):
        from typer.testing import CliRunner

        from restaurant_ai.cli import app

        monkeypatch.setenv("APPROVAL_API_KEY", key)
        reset_settings_cache()

        class Response:
            status_code = 200 if listening else 500

        import httpx

        if listening:
            monkeypatch.setattr(httpx, "get", lambda url, timeout=2: Response())
        else:
            monkeypatch.setattr(
                httpx, "get", lambda url, timeout=2: (_ for _ in ()).throw(httpx.ConnectError("no"))
            )
        result = CliRunner().invoke(app, ["dashboard", "--no-open"])
        reset_settings_cache()
        return result

    def test_it_prints_a_url_that_would_actually_work(self, monkeypatch):
        result = self._run(monkeypatch, key="a-long-random-value", listening=True)

        assert result.exit_code == 0
        assert "/dashboard?key=a-long-random-value" in result.output
        assert "/dashboard/map?key=a-long-random-value" in result.output

    def test_a_correct_address_for_a_dead_server_is_its_own_confusion(self, monkeypatch):
        result = self._run(monkeypatch, key="a-long-random-value", listening=False)

        assert result.exit_code == 1
        assert "Nothing is answering" in result.output
        # The address is still printed: knowing where it will be is useful even
        # while it is not there yet.
        assert "/dashboard?key=" in result.output

    def test_no_key_is_explained_as_the_refusal_it_causes(self, monkeypatch):
        result = self._run(monkeypatch, key="", listening=True)

        assert result.exit_code == 1
        assert "refuses every request" in result.output
        assert "including yours" in result.output

    def test_the_key_is_never_printed_without_a_url_around_it(self, monkeypatch):
        """It is a password. It appears because it has to, in the one place it
        is useful, with a line saying what it is."""
        result = self._run(monkeypatch, key="sekret-value", listening=True)

        assert "Treat it like a password" in result.output
