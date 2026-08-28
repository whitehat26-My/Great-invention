"""One command that keeps the whole restaurant running.

Docker Compose is the right answer on a server. On the machine people actually
have — a Windows laptop, an old desktop behind the till — it is four terminals
kept open by hand, and the restaurant is only as alive as the most forgettable
of them. The one everyone forgets is the listener, and a closed listener window
is a deaf bot with no error anywhere.

``restaurant-ai up`` runs every long-lived process as a child of one command:
the listener, beat, the worker, the API. One window to open, one to watch, one
Ctrl-C to stop the lot.

The property that matters is the restart. This is unattended kitchen equipment:
a child that dies at 03:00 is restarted with backoff, because nobody is there
to do it by hand — and a child that dies *instantly, every time* is reported
and given up on, because restarting a process with a bad config forever is not
resilience, it is a space heater.

Exactly one listener, here as everywhere: Telegram allows a single getUpdates
at a time, so the supervisor never starts a second copy of anything.
"""

from __future__ import annotations

import contextlib
import os
import signal
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

from restaurant_ai.logging_setup import get_logger

log = get_logger(__name__)

# A child that lives this long is considered healthy, and its crash count
# forgiven. Distinguishes "died at 03:00 after nine good hours" (restart,
# quietly) from "dies on arrival" (give up, loudly).
HEALTHY_AFTER_SECONDS = 300.0

# Consecutive on-arrival deaths before the supervisor stops restarting a child.
# Enough to ride out a Postgres that is still booting; few enough that a wrong
# password is a report, not a week of hot looping.
MAX_RAPID_DEATHS = 5

_BACKOFF_START = 2.0
_BACKOFF_CAP = 60.0


def default_children(include_api: bool = True) -> dict[str, list[str]]:
    """The processes a running restaurant is made of.

    Launched as ``python -m`` against this interpreter, so children run the
    same code and virtualenv as the supervisor — on Windows exactly as on
    Linux, with no PATH lottery over which ``celery`` gets found.
    """
    python = sys.executable
    children = {
        "listener": [python, "-m", "restaurant_ai.cli", "telegram-listen"],
        "worker": [
            python,
            "-m",
            "celery",
            "-A",
            "restaurant_ai.worker.celery_app:celery_app",
            "worker",
            "-l",
            "info",
        ],
        "beat": [
            python,
            "-m",
            "celery",
            "-A",
            "restaurant_ai.worker.celery_app:celery_app",
            "beat",
            "-l",
            "info",
        ],
    }
    if include_api:
        children["api"] = [
            python,
            "-m",
            "uvicorn",
            "restaurant_ai.api.main:app",
            "--host",
            "0.0.0.0",
            "--port",
            "8000",
        ]
    return children


class SupervisedProcess(Protocol):
    """What the supervisor needs from a process. Popen-shaped on purpose."""

    @property
    def pid(self) -> int: ...
    def poll(self) -> int | None: ...
    def wait(self, timeout: float | None = None) -> int: ...
    def terminate(self) -> None: ...
    def kill(self) -> None: ...


class GroupProcess:
    """A child in its own process group, stopped as a group.

    Two problems, one mechanism. Ctrl-C in a terminal goes to the whole
    foreground group — supervisor and children at once — so Celery starts its
    own warm shutdown just as the supervisor sends SIGTERM on top, which Celery
    reads as "cold shutdown, now", and its pool children are left behind as
    orphans. Observed, not theorised: five processes still running after a
    clean-looking stop.

    A new session detaches the child from the terminal, so Ctrl-C reaches only
    the supervisor and shutdown has one author. And signalling the *group*
    means a worker's forked pool goes down with the worker: an orphan is not
    possible, rather than merely unlikely.

    On Windows there are no process groups to escape into — Celery runs a solo
    pool there — so plain terminate() is the whole story.
    """

    def __init__(self, command: list[str]) -> None:
        if os.name == "posix":
            self._process = subprocess.Popen(command, start_new_session=True)
        else:  # pragma: no cover - exercised only on Windows
            self._process = subprocess.Popen(
                command, creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            )

    @property
    def pid(self) -> int:
        return self._process.pid

    def poll(self) -> int | None:
        return self._process.poll()

    def wait(self, timeout: float | None = None) -> int:
        return self._process.wait(timeout=timeout)

    def _signal_group(self, signum: int) -> None:
        # ProcessLookupError means already gone, which is what we wanted.
        with contextlib.suppress(ProcessLookupError):
            os.killpg(self._process.pid, signum)  # session leader: pgid == pid

    def terminate(self) -> None:
        if os.name == "posix":
            self._signal_group(signal.SIGTERM)
        else:  # pragma: no cover
            self._process.terminate()

    def kill(self) -> None:
        if os.name == "posix":
            self._signal_group(signal.SIGKILL)
        else:  # pragma: no cover
            self._process.kill()


@dataclass
class Child:
    """One supervised process and what the supervisor knows about it."""

    name: str
    command: list[str]
    process: SupervisedProcess | None = None
    started_at: float = 0.0
    rapid_deaths: int = 0
    backoff: float = _BACKOFF_START
    restart_at: float = 0.0  # 0 = start now
    abandoned: bool = False
    restarts: int = 0


class Supervisor:
    """Start, watch, restart, and stop a set of children.

    ``spawn`` and ``clock`` are injectable so the tests can drive time and
    processes without sleeping through real backoff.
    """

    def __init__(
        self,
        children: dict[str, list[str]],
        spawn: Callable[[list[str]], SupervisedProcess] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.children = [Child(name=name, command=command) for name, command in children.items()]
        self._spawn = spawn or GroupProcess
        self._clock = clock

    # -- lifecycle ----------------------------------------------------------

    def start_all(self) -> None:
        for child in self.children:
            self._start(child)

    def _start(self, child: Child) -> None:
        child.process = self._spawn(child.command)
        child.started_at = self._clock()
        child.restart_at = 0.0
        log.info("started", child=child.name, pid=child.process.pid)

    def check(self) -> None:
        """One pass over the children: restart the dead, honour backoff."""
        now = self._clock()
        for child in self.children:
            if child.abandoned:
                continue

            if child.process is not None:
                code = child.process.poll()
                if code is None:
                    # Alive. A long life earns back a clean slate.
                    if now - child.started_at >= HEALTHY_AFTER_SECONDS and (
                        child.rapid_deaths or child.backoff != _BACKOFF_START
                    ):
                        child.rapid_deaths = 0
                        child.backoff = _BACKOFF_START
                    continue

                lived = now - child.started_at
                child.process = None
                if lived < HEALTHY_AFTER_SECONDS:
                    child.rapid_deaths += 1
                else:
                    child.rapid_deaths = 1  # first fast death after a healthy run

                if child.rapid_deaths >= MAX_RAPID_DEATHS:
                    child.abandoned = True
                    log.error(
                        "giving up on a child that dies on arrival",
                        child=child.name,
                        exit_code=code,
                        deaths=child.rapid_deaths,
                        hint="its config is wrong — run `restaurant-ai doctor`, fix, restart `up`",
                    )
                    continue

                child.restart_at = now + child.backoff
                log.warning(
                    "child died — restarting",
                    child=child.name,
                    exit_code=code,
                    lived_seconds=round(lived, 1),
                    retry_in=child.backoff,
                )
                child.backoff = min(child.backoff * 2, _BACKOFF_CAP)

            elif child.restart_at and now >= child.restart_at:
                child.restarts += 1
                self._start(child)

    def stop_all(self, grace_seconds: float = 10.0) -> None:
        """Ctrl-C for everyone: terminate, wait, then insist."""
        for child in self.children:
            if child.process is not None and child.process.poll() is None:
                child.process.terminate()
        deadline = time.monotonic() + grace_seconds
        for child in self.children:
            if child.process is None:
                continue
            remaining = max(0.1, deadline - time.monotonic())
            try:
                child.process.wait(timeout=remaining)
            except subprocess.TimeoutExpired:
                child.process.kill()
                child.process.wait()
            log.info("stopped", child=child.name)

    # -- reporting ----------------------------------------------------------

    @property
    def all_abandoned(self) -> bool:
        return all(child.abandoned for child in self.children)

    def status(self) -> list[dict[str, Any]]:
        return [
            {
                "name": child.name,
                "alive": child.process is not None and child.process.poll() is None,
                "restarts": child.restarts,
                "abandoned": child.abandoned,
            }
            for child in self.children
        ]


def run(children: dict[str, list[str]], poll_seconds: float = 1.0) -> int:
    """Supervise until interrupted. Returns an exit code.

    Blocks forever in the healthy case — that is the product: the window this
    runs in *is* the restaurant being on.
    """
    supervisor = Supervisor(children)
    supervisor.start_all()
    try:
        while True:
            time.sleep(poll_seconds)
            supervisor.check()
            if supervisor.all_abandoned:
                log.error("every child has been given up on — nothing left to supervise")
                return 1
    except KeyboardInterrupt:
        log.info("stopping everything")
        supervisor.stop_all()
        return 0
