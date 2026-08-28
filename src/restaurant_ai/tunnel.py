"""A public address for the dashboard, without opening a port.

Telegram works from anywhere because the listener dials out. The dashboard and
the system map are the opposite: a browser has to reach in, which on a laptop
behind a home router means port forwarding, a domain, and a certificate — three
things to get wrong, one of which is a hole in the restaurant's network.

A Cloudflare quick tunnel dials out too. `cloudflared` opens a connection to
Cloudflare and Cloudflare answers HTTPS on a public name at the other end, so
there is still no inbound port, no domain to buy and no certificate to renew.

The catch is that a quick tunnel's address is random and changes every restart,
which makes it useless if the owner has to go and find it. So this reads the
address out of cloudflared's own output and sends it to the approvals chat: the
link arrives on the phone that is going to open it.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import time
from collections.abc import Callable

from restaurant_ai.logging_setup import get_logger

log = get_logger(__name__)

# cloudflared prints the address once, inside a box, on stderr.
_URL = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")

# It has connected and told us the address, or it has not; a minute is generous
# even on restaurant wifi, and waiting forever would hide a cloudflared that is
# never going to answer.
_ADDRESS_TIMEOUT = 60.0


class TunnelUnavailable(RuntimeError):
    """cloudflared is not installed, or never announced an address."""


def installed() -> bool:
    return shutil.which("cloudflared") is not None


def install_hint() -> str:
    return (
        "cloudflared is not installed.\n"
        "  Windows:  winget install --id Cloudflare.cloudflared\n"
        "  macOS:    brew install cloudflared\n"
        "  Linux:    see developers.cloudflare.com/cloudflare-one/connections/"
        "connect-networks/downloads/"
    )


def start(
    port: int = 8000,
    spawn: Callable[[list[str]], subprocess.Popen] | None = None,
    timeout: float = _ADDRESS_TIMEOUT,
) -> tuple[subprocess.Popen, str]:
    """Open a quick tunnel and return it with the address it was given.

    The process is returned rather than owned, so the supervisor can watch it
    and stop it with everything else.
    """
    if not installed():
        raise TunnelUnavailable(install_hint())

    command = ["cloudflared", "tunnel", "--url", f"http://localhost:{port}"]
    launch = spawn or (
        lambda cmd: subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1
        )
    )
    process = launch(command)

    deadline = time.monotonic() + timeout
    assert process.stdout is not None
    while time.monotonic() < deadline:
        line = process.stdout.readline()
        if not line:
            if process.poll() is not None:
                raise TunnelUnavailable(
                    f"cloudflared stopped before giving an address (exit {process.returncode})."
                )
            continue
        found = _URL.search(line)
        if found:
            address = found.group(0)
            log.info("tunnel open", url=address, port=port)
            return process, address

    process.terminate()
    raise TunnelUnavailable(
        "cloudflared did not announce an address within a minute — check the network."
    )


def announce(address: str, key: str) -> bool:
    """Send the links to the approvals chat, where the phone already is.

    The key goes in the URL because a browser address bar cannot set a header,
    which is the same reason the dashboard accepts `?key=`. It means the link
    *is* the credential: anyone it is forwarded to can read the restaurant's
    numbers.
    """
    from restaurant_ai.approvals.telegram import api
    from restaurant_ai.config import get_settings

    settings = get_settings()
    if not settings.telegram_bot_token or not settings.telegram_chat_id:
        return False

    api(
        "sendMessage",
        chat_id=settings.telegram_chat_id,
        text=(
            f"The dashboard is reachable from anywhere:\n\n"
            f"{address}/dashboard?key={key}\n\n"
            f"The system map:\n{address}/dashboard/map?key={key}\n\n"
            "This address lasts until the tunnel restarts, and the link carries "
            "the key — treat it like a password and do not forward it."
        ),
    )
    return True
