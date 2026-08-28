"""A public address for the dashboard.

`cloudflared` is not installed here and the tests never call it: what is worth
pinning is that the address is read out of its output rather than assumed, that
a tunnel which never opens fails loudly instead of hanging, and that a public
address is refused when it would publish an unguarded system.
"""

from __future__ import annotations

import pytest

from restaurant_ai import tunnel


class FakeCloudflared:
    """cloudflared, scripted: a few lines of noise, maybe an address."""

    def __init__(self, lines: list[str], exit_code: int | None = None) -> None:
        self._lines = list(lines)
        self.returncode = exit_code
        self.terminated = False
        self.stdout = self

    def readline(self) -> str:
        return self._lines.pop(0) if self._lines else ""

    def poll(self):
        return self.returncode if not self._lines else None

    def terminate(self) -> None:
        self.terminated = True

    def wait(self, timeout=None):
        return self.returncode or 0


class TestFindingTheAddress:
    def test_the_url_is_read_out_of_cloudflareds_own_output(self, monkeypatch):
        """Not constructed or guessed — quick-tunnel names are random."""
        monkeypatch.setattr(tunnel, "find_cloudflared", lambda: "cloudflared")
        banner = [
            "INF Requesting new quick Tunnel on trycloudflare.com...\n",
            "INF +----------------------------------------+\n",
            "INF |  https://brave-tiger-fresh-mint.trycloudflare.com  |\n",
            "INF +----------------------------------------+\n",
        ]
        process, address = tunnel.start(spawn=lambda cmd: FakeCloudflared(banner))
        assert address == "https://brave-tiger-fresh-mint.trycloudflare.com"

    def test_the_local_port_is_the_one_asked_for(self, monkeypatch):
        monkeypatch.setattr(tunnel, "find_cloudflared", lambda: "cloudflared")
        seen = {}

        def spawn(cmd):
            seen["cmd"] = cmd
            return FakeCloudflared(["https://x-y-z.trycloudflare.com\n"])

        tunnel.start(port=9123, spawn=spawn)
        assert "http://localhost:9123" in seen["cmd"]

    def test_a_cloudflared_that_dies_says_so_rather_than_hanging(self, monkeypatch):
        monkeypatch.setattr(tunnel, "find_cloudflared", lambda: "cloudflared")
        with pytest.raises(tunnel.TunnelUnavailable, match="stopped before"):
            tunnel.start(spawn=lambda cmd: FakeCloudflared([], exit_code=1))

    def test_silence_is_given_up_on_rather_than_waited_out_forever(self, monkeypatch):
        monkeypatch.setattr(tunnel, "find_cloudflared", lambda: "cloudflared")
        chatty = FakeCloudflared(["INF connecting\n"] * 3)
        with pytest.raises(tunnel.TunnelUnavailable, match="did not announce"):
            tunnel.start(spawn=lambda cmd: chatty, timeout=0.2)
        assert chatty.terminated, "a tunnel we gave up on must not be left running"

    def test_a_missing_cloudflared_names_how_to_install_it(self, monkeypatch):
        monkeypatch.setattr(tunnel, "find_cloudflared", lambda: None)
        with pytest.raises(tunnel.TunnelUnavailable, match="Invoke-WebRequest"):
            tunnel.start()

    def test_the_hint_does_not_assume_winget_exists(self, monkeypatch):
        """Older Windows installs have never heard of winget."""
        hint = tunnel.install_hint()
        assert "Invoke-WebRequest" in hint
        assert hint.index("Invoke-WebRequest") < hint.index("winget")


class TestFindingCloudflared:
    def test_the_one_on_the_path_is_used(self, monkeypatch):
        monkeypatch.setattr(tunnel.shutil, "which", lambda name: "/usr/bin/cloudflared")
        assert tunnel.find_cloudflared() == "/usr/bin/cloudflared"

    def test_a_copy_downloaded_into_the_project_folder_is_found(self, monkeypatch, tmp_path):
        """The winget-less fallback puts the .exe right here."""
        monkeypatch.setattr(tunnel.shutil, "which", lambda name: None)
        (tmp_path / "cloudflared.exe").write_bytes(b"")
        monkeypatch.chdir(tmp_path)

        assert tunnel.find_cloudflared() == str(tmp_path / "cloudflared.exe")

    def test_the_full_path_is_what_gets_run(self, monkeypatch, tmp_path):
        """Not the bare name: relying on Windows searching the working
        directory is relying on a default that has been tightened before."""
        monkeypatch.setattr(tunnel.shutil, "which", lambda name: None)
        (tmp_path / "cloudflared.exe").write_bytes(b"")
        monkeypatch.chdir(tmp_path)

        seen = {}

        def spawn(cmd):
            seen["cmd"] = cmd
            return FakeCloudflared(["https://a-b.trycloudflare.com\n"])

        tunnel.start(spawn=spawn)
        assert seen["cmd"][0] == str(tmp_path / "cloudflared.exe")

    def test_nothing_anywhere_is_nothing(self, monkeypatch, tmp_path):
        monkeypatch.setattr(tunnel.shutil, "which", lambda name: None)
        monkeypatch.chdir(tmp_path)
        assert tunnel.find_cloudflared() is None


class TestTheAnnouncement:
    def test_both_links_carry_the_key_and_a_warning(self, monkeypatch):
        from restaurant_ai.config import reset_settings_cache

        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
        monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")
        reset_settings_cache()

        sent = {}
        monkeypatch.setattr(
            "restaurant_ai.approvals.telegram.api",
            lambda method, **kw: sent.update(kw) or {"ok": True},
        )
        assert tunnel.announce("https://a-b-c.trycloudflare.com", "the-key") is True

        text = sent["text"]
        assert "/dashboard?key=the-key" in text
        assert "/dashboard/map?key=the-key" in text
        # The link is the credential; say so where it is handed over.
        assert "like a password" in text
        reset_settings_cache()

    def test_without_telegram_it_declines_rather_than_failing(self, monkeypatch):
        from restaurant_ai.config import reset_settings_cache

        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "")
        reset_settings_cache()
        assert tunnel.announce("https://a.trycloudflare.com", "k") is False
        reset_settings_cache()


class TestNothingUnguardedGoesPublic:
    """The audit that had to happen before any of this existed."""

    def test_reading_agent_runs_needs_the_key(self, db, monkeypatch):
        """A run summary is the restaurant's business, and this was open.

        Harmless while it only ever answered on localhost; not harmless the
        moment a tunnel gives it a public address.
        """
        from fastapi.testclient import TestClient

        from restaurant_ai.api.main import app
        from restaurant_ai.config import reset_settings_cache

        monkeypatch.setenv("APPROVAL_API_KEY", "a-configured-key")
        reset_settings_cache()
        with TestClient(app) as client:
            assert client.get("/agents/runs").status_code == 401
            assert client.get("/agents").status_code == 401
            assert (
                client.get("/agents", headers={"X-API-Key": "a-configured-key"}).status_code == 200
            )
        reset_settings_cache()

    def test_every_endpoint_that_returns_data_is_closed(self, db, monkeypatch):
        """Enumerated from the app's own schema, so a new route joins this."""
        from fastapi.testclient import TestClient

        from restaurant_ai.api.main import app
        from restaurant_ai.config import reset_settings_cache

        monkeypatch.setenv("APPROVAL_API_KEY", "a-configured-key")
        reset_settings_cache()

        # Deliberately public: liveness probes, and the docs describe shape, not data.
        public = {"/", "/health", "/ready", "/docs", "/redoc", "/openapi.json"}

        with TestClient(app) as client:
            spec = client.get("/openapi.json").json()
            for path, methods in spec["paths"].items():
                if path in public or "{" in path:
                    continue
                for method in methods:
                    if method.upper() not in ("GET", "POST"):
                        continue
                    response = client.request(
                        method.upper(), path, json={} if method == "post" else None
                    )
                    assert response.status_code in (401, 403, 503), (
                        f"{method.upper()} {path} answered {response.status_code} "
                        "without credentials"
                    )
        reset_settings_cache()
