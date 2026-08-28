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


class TestTheSystemMap:
    """The pressable map: the brain, six departments, and every agent's detail."""

    def test_no_key_configured_refuses(self, anonymous):
        assert anonymous.get("/dashboard/map").status_code == 503
        assert anonymous.get("/dashboard/map/data").status_code == 503

    def test_a_wrong_key_is_refused(self, client):
        assert client.get("/dashboard/map?key=nope").status_code == 401
        assert client.get("/dashboard/map/data?key=nope").status_code == 401

    def test_the_map_page_is_an_empty_shell(self, client):
        page = client.get(f"/dashboard/map?key={KEY}")
        assert page.status_code == 200
        assert "/dashboard/map/data" in page.text
        # No agent identity or brief text is baked into the page itself.
        assert "Rain" not in page.text
        assert "purchase order" not in page.text.lower()

    def test_the_map_describes_the_system_the_registry_declares(self, client):
        data = client.get(f"/dashboard/map/data?key={KEY}").json()
        assert set(data) == {"restaurant", "departments"}

        departments = {d["name"]: d for d in data["departments"]}
        assert set(departments) == {
            "front_of_house",
            "kitchen",
            "supply",
            "marketing",
            "workforce",
            "finance",
        }
        agents = [a for d in data["departments"] for a in d["agents"]]
        assert len(agents) == 11

        for agent in agents:
            assert set(agent) == {
                "name",
                "person",
                "title",
                "description",
                "model_tier",
                "schedule",
                "brief",
                "status",
                "last_summary",
                "tools",
            }
            # "How the agent works" must never be blank on the panel.
            assert agent["brief"].strip()
            assert agent["description"].strip()
            assert agent["schedule"].strip()
            for tool in agent["tools"]:
                assert set(tool) == {"name", "description", "gated"}

    def test_gated_tools_are_marked_as_needing_the_owner(self, client):
        """Rain drafts POs behind the approval gate; the map must say so."""
        data = client.get(f"/dashboard/map/data?key={KEY}").json()
        rain = next(a for d in data["departments"] for a in d["agents"] if a["person"] == "Rain")
        gated = {t["name"] for t in rain["tools"] if t["gated"]}
        assert "draft_purchase_orders" in gated

    def test_every_department_label_is_human(self, client):
        data = client.get(f"/dashboard/map/data?key={KEY}").json()
        for dept in data["departments"]:
            assert dept["label"] and "_" not in dept["label"]

    def test_the_dashboard_links_to_the_map(self, client):
        page = client.get(f"/dashboard?key={KEY}")
        assert "/dashboard/map" in page.text
