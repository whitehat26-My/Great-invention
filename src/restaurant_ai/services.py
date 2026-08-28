"""The services under everything, and how ``up`` makes sure they exist.

Two failures came back from the first Windows deployment, both mine:

``restaurant-ai up`` refused because Postgres was not running, and its advice
was to run a command — fair on a server, useless on a laptop where the honest
next question is "what is Postgres and why is it my job?". If Docker is present
and awake, starting the data services *is* the supervisor's job; and when it
cannot, the three reasons (no Docker, Docker asleep, containers failed) have
three different fixes and deserve three different sentences.

``schtasks`` answered "Access is denied", because it needs an elevated prompt.
The per-user Startup folder needs nothing: a program can install itself there.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

from restaurant_ai.logging_setup import get_logger

log = get_logger(__name__)


# ---------------------------------------------------------------------------
# Is the database there, and can we make it be?
# ---------------------------------------------------------------------------


def database_error() -> str | None:
    """None if Postgres answers, otherwise the reason it did not."""
    from sqlalchemy import text

    from restaurant_ai.db.base import get_engine

    try:
        with get_engine().connect() as conn:
            conn.execute(text("select 1"))
        return None
    except Exception as exc:
        return f"{type(exc).__name__}: {exc}"


def docker_state() -> str:
    """``absent``, ``engine-down`` or ``ready``.

    "Docker is installed" and "Docker is running" are different facts with
    different fixes, and on Windows the second is the common one: Docker
    Desktop is an application somebody has to have opened.
    """
    if shutil.which("docker") is None:
        return "absent"
    try:
        probe = subprocess.run(["docker", "info"], capture_output=True, timeout=30)
    except Exception:
        return "engine-down"
    return "ready" if probe.returncode == 0 else "engine-down"


def compose_dir(start: Path | None = None) -> Path | None:
    """The folder holding docker-compose.yml, from here upward."""
    here = start or Path.cwd()
    for folder in [here, *here.parents]:
        if (folder / "docker-compose.yml").exists():
            return folder
    return None


def start_data_services(folder: Path) -> bool:
    """``docker compose up -d postgres redis``, output left visible.

    Not captured on purpose: the first run pulls images, which on restaurant
    wifi takes minutes, and a silent minutes-long command looks hung.
    """
    result = subprocess.run(["docker", "compose", "up", "-d", "postgres", "redis"], cwd=folder)
    return result.returncode == 0


def wait_for_database(deadline_seconds: float = 120.0) -> str | None:
    """Poll until Postgres answers or the deadline passes. None on success."""
    deadline = time.monotonic() + deadline_seconds
    error = database_error()
    while error is not None and time.monotonic() < deadline:
        time.sleep(2)
        error = database_error()
    return error


def ensure_database() -> tuple[bool, str]:
    """Postgres reachable, starting it ourselves if Docker makes that possible.

    Returns (ok, message). Every False carries the fix for the situation the
    machine is actually in, because "start it first" was already tried and
    answered with "how?".
    """
    error = database_error()
    if error is None:
        return True, "Postgres is up."

    state = docker_state()
    if state == "absent":
        return False, (
            f"Postgres is not reachable ({error.splitlines()[0]}), and Docker is not "
            "installed, so I cannot start it for you.\n"
            "  Install Docker Desktop (docker.com), open it once, then run "
            "`restaurant-ai up` again — it will start the database itself from then on."
        )
    if state == "engine-down":
        return False, (
            "Postgres is not reachable, and Docker is installed but not running.\n"
            "  Open Docker Desktop from the Start menu, wait for it to say it is "
            "running, then run `restaurant-ai up` again."
        )

    folder = compose_dir()
    if folder is None:
        return False, (
            "Postgres is not reachable, and there is no docker-compose.yml here to "
            "start it from.\n  Run this from the Great-invention folder."
        )

    log.info("starting postgres and redis via docker compose", cwd=str(folder))
    if not start_data_services(folder):
        return False, ("Docker could not start postgres/redis — its output above says why.")

    error = wait_for_database()
    if error is not None:
        return False, (
            "The containers started but Postgres did not answer within two minutes "
            f"({error.splitlines()[0]}).\n  `docker compose logs postgres` says what "
            "it is unhappy about."
        )
    return True, "Started Postgres and Redis with Docker, and Postgres is up."


# ---------------------------------------------------------------------------
# Opening the window at logon, without administrator rights
# ---------------------------------------------------------------------------


def startup_folder() -> Path | None:
    """The per-user Windows Startup folder, which needs no elevation.

    ``schtasks`` was the first suggestion and it answered "Access is denied":
    creating a scheduled task needs an elevated prompt. Everything dropped in
    this folder simply runs at logon, and it belongs to the user already.
    """
    appdata = os.environ.get("APPDATA")
    if not appdata:
        return None
    return Path(appdata) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"


_LAUNCHER = "the-great-invention.bat"


def write_startup_script(project_dir: Path, folder: Path) -> Path:
    """A launcher that opens the restaurant's window at logon.

    ``cd`` matters: settings are read from ``.env`` in the working directory,
    so the launcher must start where the owner configured the restaurant, not
    where Windows happens to start batch files. ``pause`` at the end means a
    launcher that dies on arrival leaves its reason on the screen instead of a
    window that flashes and vanishes.
    """
    folder.mkdir(parents=True, exist_ok=True)
    script = folder / _LAUNCHER
    script.write_text(
        "@echo off\n"
        "title The Great Invention\n"
        f'cd /d "{project_dir}"\n'
        f'"{sys.executable}" -m restaurant_ai.cli up\n'
        "pause\n",
        encoding="utf-8",
    )
    return script


def remove_startup_script(folder: Path) -> bool:
    script = folder / _LAUNCHER
    if script.exists():
        script.unlink()
        return True
    return False
