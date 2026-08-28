"""One window that is the restaurant being on.

The two properties worth pinning are the two halves of the same judgement: a
child that dies after a healthy run is restarted without a human, and a child
that dies on arrival every time is given up on with a report — because
restarting a bad config forever is not resilience.
"""

from __future__ import annotations

import sys

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
