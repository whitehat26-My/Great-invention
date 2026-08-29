"""One window that is the restaurant being on.

The two properties worth pinning are the two halves of the same judgement: a
child that dies after a healthy run is restarted without a human, and a child
that dies on arrival every time is given up on with a report — because
restarting a bad config forever is not resilience.
"""

from __future__ import annotations

import sys

from restaurant_ai.config import reset_settings_cache
from restaurant_ai.supervisor import (
    HEALTHY_AFTER_SECONDS,
    MAX_RAPID_DEATHS,
    Supervisor,
    default_children,
)


class FakeProcess:
    """A Popen the test scripts: alive until told, then exits with a code."""

    _next_pid = 1000

    def __init__(self) -> None:
        FakeProcess._next_pid += 1
        self.pid = FakeProcess._next_pid
        self.exit_code: int | None = None
        self.terminated = False
        self.killed = False

    def poll(self) -> int | None:
        return self.exit_code

    def dies(self, code: int = 1) -> None:
        self.exit_code = code

    def terminate(self) -> None:
        self.terminated = True
        self.exit_code = -15

    def kill(self) -> None:
        self.killed = True
        self.exit_code = -9

    def wait(self, timeout: float | None = None) -> int:
        assert self.exit_code is not None
        return self.exit_code


class Bench:
    """A supervisor on a hand-cranked clock, spawning fake processes."""

    def __init__(self, names: list[str] | None = None) -> None:
        self.now = 0.0
        self.spawned: list[FakeProcess] = []

        def spawn(command):
            process = FakeProcess()
            self.spawned.append(process)
            return process

        self.supervisor = Supervisor(
            {name: ["cmd", name] for name in (names or ["listener"])},
            spawn=spawn,
            clock=lambda: self.now,
        )

    def tick(self, seconds: float = 1.0) -> None:
        self.now += seconds
        self.supervisor.check()

    @property
    def child(self):
        return self.supervisor.children[0]


class TestRestarting:
    def test_a_death_after_a_healthy_run_is_restarted(self):
        """Died at 03:00 after nine good hours: the case this exists for."""
        bench = Bench()
        bench.supervisor.start_all()
        bench.tick(HEALTHY_AFTER_SECONDS + 10)

        bench.spawned[0].dies(1)
        bench.tick()  # noticed, restart scheduled
        bench.tick(2)  # backoff elapsed

        assert len(bench.spawned) == 2
        assert bench.child.restarts == 1
        assert not bench.child.abandoned

    def test_the_restart_waits_out_the_backoff(self):
        """A dead child is not restarted in the same breath it died in."""
        bench = Bench()
        bench.supervisor.start_all()
        bench.tick(HEALTHY_AFTER_SECONDS + 10)

        bench.spawned[0].dies(1)
        bench.tick()
        assert len(bench.spawned) == 1  # scheduled, not yet started
        bench.tick(0.5)
        assert len(bench.spawned) == 1  # still inside the backoff
        bench.tick(2)
        assert len(bench.spawned) == 2

    def test_repeated_deaths_back_off_further_each_time(self):
        bench = Bench()
        bench.supervisor.start_all()
        waits = []
        for _ in range(3):
            bench.spawned[-1].dies(1)
            before = bench.child.backoff
            bench.tick()
            waits.append(before)
            bench.tick(before + 1)  # let it restart
        assert waits == sorted(waits)
        assert waits[0] < waits[-1]

    def test_a_long_healthy_life_earns_back_a_clean_slate(self):
        """Nine good hours forgive last week's crashes."""
        bench = Bench()
        bench.supervisor.start_all()

        bench.spawned[0].dies(1)
        bench.tick()
        bench.tick(3)  # restarted; rapid_deaths == 1, backoff grown
        assert bench.child.rapid_deaths == 1

        bench.tick(HEALTHY_AFTER_SECONDS + 10)
        assert bench.child.rapid_deaths == 0
        assert bench.child.backoff == 2.0


class TestGivingUp:
    def test_a_child_that_dies_on_arrival_is_abandoned_not_hot_looped(self):
        bench = Bench()
        bench.supervisor.start_all()

        for _ in range(MAX_RAPID_DEATHS):
            bench.spawned[-1].dies(1)
            bench.tick()
            bench.tick(bench.child.backoff + 61)  # ride out any backoff

        assert bench.child.abandoned
        spawned_before = len(bench.spawned)
        bench.tick(120)
        assert len(bench.spawned) == spawned_before  # never restarted again

    def test_one_bad_child_does_not_take_down_the_good_ones(self):
        """The listener with a bad token must not stop beat from scheduling."""
        bench = Bench(["listener", "beat"])
        bench.supervisor.start_all()

        for _ in range(MAX_RAPID_DEATHS):
            # kill only the listener's current process; beat stays healthy
            bench.supervisor.children[0].process.dies(1)
            bench.tick()
            bench.tick(65)

        listener_child, beat_child = bench.supervisor.children
        assert listener_child.abandoned
        assert not beat_child.abandoned
        assert beat_child.process.poll() is None

    def test_everything_abandoned_is_reported_as_such(self):
        bench = Bench()
        bench.supervisor.start_all()
        for _ in range(MAX_RAPID_DEATHS):
            bench.supervisor.children[0].process.dies(1)
            bench.tick()
            bench.tick(65)
        assert bench.supervisor.all_abandoned


class TestStopping:
    def test_ctrl_c_terminates_every_child(self):
        bench = Bench(["listener", "beat", "worker"])
        bench.supervisor.start_all()
        bench.supervisor.stop_all(grace_seconds=0.5)
        assert all(p.terminated for p in bench.spawned)


class TestTheProcessList:
    def test_exactly_one_listener(self):
        """Telegram allows one getUpdates; two listeners each drop half."""
        children = default_children()
        listeners = [n for n in children if n == "listener"]
        assert listeners == ["listener"]

    def test_children_run_this_interpreter_not_the_path_lottery(self):
        """On Windows, `celery` on PATH may belong to another Python entirely."""
        for command in default_children().values():
            assert command[0] == sys.executable
            assert command[1] == "-m"

    def test_the_api_can_be_left_out(self):
        assert "api" not in default_children(include_api=False)
        assert "listener" in default_children(include_api=False)

    def test_every_module_named_is_importable(self):
        """The supervisor must not discover a typo at 03:00."""
        import importlib

        for command in default_children().values():
            importlib.import_module(command[2])


class TestNothingOutlivesTheSupervisor:
    def test_children_are_stopped_even_when_the_loop_itself_crashes(self, monkeypatch):
        """Children live in their own sessions, so nobody else would reap them.

        The real case: `up | head` closed stdout, a log write raised
        BrokenPipeError mid-loop, and seven processes outlived their
        supervisor. However the loop ends, the children end with it.
        """
        import pytest

        from restaurant_ai import supervisor as supervisor_module

        spawned: list[FakeProcess] = []

        def spawn(command):
            process = FakeProcess()
            spawned.append(process)
            return process

        monkeypatch.setattr(supervisor_module, "GroupProcess", spawn)
        monkeypatch.setattr(
            supervisor_module.Supervisor,
            "check",
            lambda self: (_ for _ in ()).throw(BrokenPipeError("stdout went away")),
        )
        monkeypatch.setattr(supervisor_module.time, "sleep", lambda s: None)

        with pytest.raises(BrokenPipeError):
            supervisor_module.run({"listener": ["cmd"], "beat": ["cmd"]}, poll_seconds=0)

        assert spawned, "children were started"
        assert all(p.terminated for p in spawned)


class TestTheTunnelIsSupervisedToo:
    """The second window is the one that gets closed.

    That is the whole reason `up` exists — the listener taught it, and the
    tunnel had the same shape: a separate command, in its own window, that
    someone has to remember. Nothing reports its absence, and the dashboard is
    simply unreachable from outside with no error anywhere.
    """

    def test_it_is_absent_unless_asked_for(self):
        assert "tunnel" not in default_children()

    def test_asking_for_it_adds_it(self):
        children = default_children(include_tunnel=True)
        assert "tunnel" in children
        assert children["tunnel"][1:] == ["-m", "restaurant_ai.tunnel"]

    def test_it_runs_on_this_interpreter(self):
        """Same virtualenv as everything else, no PATH lottery."""
        import sys

        assert default_children(include_tunnel=True)["tunnel"][0] == sys.executable


class TestUpRefusesATunnelItCannotOpen:
    """Before anything starts, not from a line buried in four processes' logs.

    An owner who asked for a public address and did not get one should be told
    so, rather than discovering it when the link never arrives on their phone.
    """

    def _run(self, monkeypatch, **env):
        from typer.testing import CliRunner

        from restaurant_ai.cli import app

        monkeypatch.setattr(
            "restaurant_ai.services.ensure_database", lambda: (True, "Postgres is up.")
        )
        monkeypatch.setattr(
            "restaurant_ai.services.migrate_database", lambda: (True, "Schema is up to date.")
        )
        monkeypatch.setattr("restaurant_ai.supervisor.run", lambda children: 0)
        for key, value in env.items():
            monkeypatch.setenv(key, value)
        reset_settings_cache()
        return CliRunner().invoke(app, ["up", "--with-tunnel"])

    def test_without_a_key_it_says_what_would_be_published(self, monkeypatch):
        monkeypatch.setenv("APPROVAL_API_KEY", "")
        monkeypatch.setattr("restaurant_ai.tunnel.installed", lambda: True)
        result = self._run(monkeypatch)

        assert result.exit_code == 1
        assert "locked door" in result.output
        reset_settings_cache()

    def test_without_cloudflared_it_says_how_to_get_it(self, monkeypatch):
        monkeypatch.setattr("restaurant_ai.tunnel.installed", lambda: False)
        result = self._run(monkeypatch, APPROVAL_API_KEY="a-long-random-value")

        assert result.exit_code == 1
        assert "cloudflared is not installed" in result.output
        reset_settings_cache()

    def test_it_starts_when_both_are_in_place(self, monkeypatch):
        monkeypatch.setattr("restaurant_ai.tunnel.installed", lambda: True)
        result = self._run(monkeypatch, APPROVAL_API_KEY="a-long-random-value")

        assert result.exit_code == 0
        assert "public link goes to the approvals chat" in result.output
        reset_settings_cache()
