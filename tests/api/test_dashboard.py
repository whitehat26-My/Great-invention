"""The live dashboard — same fail-closed posture as the rest of the surface."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from restaurant_ai.api.main import app
from restaurant_ai.config import reset_settings_cache

pytestmark = pytest.mark.db

KEY = "dash-test-key"


@pytest.fixture
def client(db, monkeypatch):
    monkeypatch.setenv("APPROVAL_API_KEY", KEY)
    reset_settings_cache()
    with TestClient(app) as c:
        yield c
    reset_settings_cache()


@pytest.fixture
def anonymous(db):
    with TestClient(app) as c:
        yield c


class TestTheDoorIsShut:
    def test_no_key_configured_refuses(self, anonymous):
        assert anonymous.get("/dashboard").status_code == 503
        assert anonymous.get("/dashboard/data").status_code == 503

    def test_a_wrong_key_is_refused(self, client):
        assert client.get("/dashboard?key=nope").status_code == 401
        assert client.get("/dashboard/data", headers={"X-API-Key": "nope"}).status_code == 401

    def test_the_page_carries_no_data_of_its_own(self, client):
        """A leaked URL without the key must show an empty shell.

        Everything arrives via the authenticated fetch, so the HTML itself can
        contain markup and script but no numbers from the database.
        """
        page = client.get(f"/dashboard?key={KEY}")
        assert page.status_code == 200
        assert "/dashboard/data" in page.text
        assert "ingredients tracked" not in page.text


class TestTheData:
    def test_query_param_and_header_both_work(self, client):
        assert client.get(f"/dashboard/data?key={KEY}").status_code == 200
        assert client.get("/dashboard/data", headers={"X-API-Key": KEY}).status_code == 200

    def test_the_shape_the_page_renders_from(self, client):
        data = client.get(f"/dashboard/data?key={KEY}").json()
        assert set(data) == {
            "restaurant",
            "generated_at",
            "business_date",
            "sections",
            "needs_you",
            "failures",
            "history",
            "agents",
        }
        assert len(data["agents"]) == 11
        assert {a["person"] for a in data["agents"]} >= {"Rain", "Camelia", "Aziera"}
        for entry in data["history"]:
            assert set(entry) >= {"date", "revenue", "covers", "prime_pct", "labour_pct"}

    def test_every_agent_status_is_renderable(self, client):
        """The page maps statuses to dots; an unknown status would render blank."""
        data = client.get(f"/dashboard/data?key={KEY}").json()
        renderable = {"idle", "completed", "awaiting_approval", "failed", "rejected", "running"}
        assert all(a["status"] in renderable for a in data["agents"])
