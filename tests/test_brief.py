"""The owner's daily brief — the one message that stitches six departments.

The property that matters most is resilience: this is read at midnight after
close, and one broken department must never cost the owner the other five.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from typer.testing import CliRunner

from restaurant_ai import brief as brief_module
from restaurant_ai import clock
from restaurant_ai.brief import Brief, build_brief, render_brief, send_brief
from restaurant_ai.db.models import DailyReport
from restaurant_ai.kernel.registry import get_agent
from restaurant_ai.kernel.runner import run_agent

pytestmark = pytest.mark.db


@pytest.fixture
def quiet(db):
    """A day with nothing hanging over from interactive use.

    These tests run against whatever database is configured, and a lived-in
    development one already carries closed days and parked approvals from
    manual runs. Scrubbing inside the test's transaction (rolled back after)
    makes the assertions about *this* test's world, not the machine's history.
    """
    from restaurant_ai.db.models import AgentRun, ApprovalRequest

    for model in (DailyReport, ApprovalRequest, AgentRun):
        for row in db.query(model).all():
            db.delete(row)
    db.flush()
    return db


class TestTheBriefHoldsTogether:
    def test_every_department_reports(self, db):
        built = build_brief(db)
        assert set(built.sections) == {
            "money",
            "supply",
            "kitchen",
            "floor",
            "marketing",
            "people",
            "agents",
        }

    def test_a_broken_section_is_a_line_not_a_crash(self, db, monkeypatch):
        """Midnight resilience: five working sections survive the sixth."""

        def explode(*args, **kwargs):
            raise RuntimeError("stock view is on fire")

        monkeypatch.setattr(brief_module, "_perceive", explode)
        built = build_brief(db)

        assert "unavailable" in built.sections["supply"][0]
        assert "stock view is on fire" in built.sections["supply"][0]
        # The neighbours are untouched.
        assert "unavailable" not in built.sections["money"][0]
        assert "unavailable" not in built.sections["people"][0]

    def test_an_unclosed_day_says_so_rather_than_guessing(self, quiet, db):
        built = build_brief(db)
        assert "not closed yet" in built.sections["money"][0]

    def test_a_closed_day_carries_camelias_verdict(self, quiet, db):
        db.add(
            DailyReport(
                business_date=clock.today(),
                net_revenue=Decimal("4169.70"),
                covers=179,
                average_check=Decimal("23.29"),
                # Two decimal places: the Money column type quantizes on
                # flush, so 0.705 would silently become 0.71 anyway.
                food_cost_pct=Decimal("0.33"),
                labour_pct=Decimal("0.37"),
                prime_cost_pct=Decimal("0.71"),
                operating_margin_pct=Decimal("0.29"),
                commentary="Prime cost 70.5% is unsustainable. More detail follows.",
            )
        )
        db.flush()

        money = build_brief(db).sections["money"]
        assert "4,169.70" in money[0] and "179 covers" in money[0]
        assert "prime 71.0%" in money[1]
        # First sentence only — the verdict, not the essay.
        assert money[2] == "Prime cost 70.5% is unsustainable."

    def test_supply_reports_what_rain_sees(self, db, stock_is_low):
        supply = build_brief(db).sections["supply"]
        assert "at/below reorder point" in supply[0]
        assert not supply[0].startswith("0 ingredients")


class TestNeedsYou:
    def test_a_parked_approval_is_surfaced_by_name(self, quiet, db, stock_is_low):
        outcome = run_agent(get_agent("stock_reorder"), trigger="test")
        assert outcome.interrupted

        built = build_brief(db)
        assert any("Rain" in line for line in built.needs_you)
        assert "awaiting approval" in built.sections["agents"][0]

    def test_a_quiet_night_says_nothing_needs_you(self, quiet, db):
        rendered = render_brief(build_brief(db))
        assert "Nothing needs you tonight." in rendered


class TestRendering:
    def test_the_message_reads_top_to_bottom(self, db):
        rendered = render_brief(build_brief(db))
        assert rendered.startswith("The Great Invention — daily brief")
        assert rendered.index("MONEY") < rendered.index("SUPPLY") < rendered.index("AGENTS")

    def test_it_never_exceeds_what_telegram_accepts(self):
        bloated = Brief(business_date=date(2026, 8, 28))
        bloated.sections["money"] = ["x" * 500] * 20
        rendered = render_brief(bloated)
        assert len(rendered) <= 4096
        assert "truncated" in rendered


class TestDelivery:
    def test_unconfigured_telegram_refuses_quietly(self, monkeypatch):
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "")
        from restaurant_ai.config import reset_settings_cache

        reset_settings_cache()
        assert send_brief("hello") is False
        reset_settings_cache()

    def test_a_configured_chat_receives_the_text(self, monkeypatch):
        from restaurant_ai.config import reset_settings_cache

        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
        monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")
        reset_settings_cache()

        sent = {}
        monkeypatch.setattr(
            "restaurant_ai.approvals.telegram.api",
            lambda method, **kw: sent.update({"method": method, **kw}) or {"ok": True},
        )
        assert send_brief("the brief text") is True
        assert sent == {"method": "sendMessage", "chat_id": "42", "text": "the brief text"}
        reset_settings_cache()


class TestTheCli:
    def test_brief_prints_without_telegram(self, db):
        from restaurant_ai.cli import app

        result = CliRunner().invoke(app, ["brief"])
        assert result.exit_code == 0, result.output
        assert "daily brief" in result.output
        assert "MONEY" in result.output

    def test_send_without_telegram_fails_loudly(self, db, monkeypatch):
        from restaurant_ai.cli import app
        from restaurant_ai.config import reset_settings_cache

        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "")
        reset_settings_cache()
        result = CliRunner().invoke(app, ["brief", "--send"])
        assert result.exit_code == 1
        assert "NOT sent" in result.output
        reset_settings_cache()


class TestTheOwnersDiaryReachesThem:
    """An approval is work an agent prepared for a person to decide. A reminder
    is work only the owner can do, that no agent will ever pick up. Both belong
    in the brief, because the brief is the one thing that reliably gets read —
    and a reminder nobody reads is not a reminder."""

    def test_something_due_appears_in_needs_you(self, db):
        from datetime import timedelta

        from restaurant_ai import reminders
        from restaurant_ai.brief import build_brief
        from restaurant_ai.db.models import Reminder

        db.query(Reminder).delete()
        today = clock.today()
        reminders.add(db, "renew the halal certificate", today + timedelta(days=2))
        db.flush()

        brief = build_brief(db, today)

        assert any("halal certificate" in line for line in brief.needs_you)
        db.query(Reminder).delete()

    def test_late_is_marked_as_late(self, db):
        """The owner scanning a list at midnight needs the costly one to stand out."""
        from datetime import timedelta

        from restaurant_ai import reminders
        from restaurant_ai.brief import build_brief
        from restaurant_ai.db.models import Reminder

        db.query(Reminder).delete()
        today = clock.today()
        reminders.add(db, "extinguisher service", today - timedelta(days=4))
        db.flush()

        brief = build_brief(db, today)

        assert any(line.startswith("LATE — ") for line in brief.needs_you)
        db.query(Reminder).delete()

    def test_a_broken_diary_does_not_lose_the_rest_of_the_brief(self, db, monkeypatch):
        from restaurant_ai import reminders
        from restaurant_ai.brief import build_brief

        monkeypatch.setattr(
            reminders, "due", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no table"))
        )
        brief = build_brief(db, clock.today())

        assert any("could not read the diary" in line for line in brief.needs_you)
        assert brief.sections, "the rest of the brief still has to arrive"
