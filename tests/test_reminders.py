"""The owner's own diary.

Everything else here is work an agent does. This is the one place the platform
holds work a *person* has to do, and Aziera's job is making sure they do it.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from restaurant_ai import reminders
from restaurant_ai.db.models import Reminder

pytestmark = pytest.mark.db

TODAY = date(2026, 3, 1)


@pytest.fixture(autouse=True)
def _clean(db):
    db.query(Reminder).delete()
    db.commit()
    yield
    db.query(Reminder).delete()
    db.commit()


class TestKeepingTheDiary:
    def test_something_written_down_comes_back(self, db):
        reminders.add(db, "renew the halal certificate", date(2026, 3, 12))

        found = reminders.due(db, on=TODAY, within=timedelta(days=30))

        assert [i.what for i in found] == ["renew the halal certificate"]

    def test_overdue_is_never_dropped(self, db):
        """A date that has passed is the reminder working hardest, not one that
        has expired."""
        reminders.add(db, "extinguisher service", date(2026, 2, 1))

        found = reminders.due(db, on=TODAY, within=timedelta(days=1))

        assert len(found) == 1
        assert found[0].overdue

    def test_the_far_future_is_not_noise_today(self, db):
        reminders.add(db, "lease renewal", date(2027, 1, 1))

        assert reminders.due(db, on=TODAY) == []
        assert len(reminders.open_items(db)) == 1

    def test_soonest_first(self, db):
        reminders.add(db, "later", TODAY + timedelta(days=5))
        reminders.add(db, "sooner", TODAY + timedelta(days=1))

        assert [i.what for i in reminders.due(db, on=TODAY)] == ["sooner", "later"]


class TestHowItIsSaid:
    """A date and a number is not how a person says this."""

    def _phrase(self, db, due_on: date) -> str:
        reminders.add(db, "the thing", due_on)
        return reminders.due(db, on=TODAY, within=timedelta(days=400))[0].phrase()

    def test_today(self, db):
        assert "— today" in self._phrase(db, TODAY)

    def test_tomorrow(self, db):
        assert "— tomorrow" in self._phrase(db, TODAY + timedelta(days=1))

    def test_days_away_names_the_date_too(self, db):
        said = self._phrase(db, TODAY + timedelta(days=11))
        assert "in 11 days" in said and "Mar" in said

    def test_overdue_says_how_long(self, db):
        assert "was due 3 days ago" in self._phrase(db, TODAY - timedelta(days=3))

    def test_one_day_is_not_pluralised(self, db):
        assert "was due 1 day ago" in self._phrase(db, TODAY - timedelta(days=1))


class TestClosingThingsOff:
    def test_done_stops_it_coming_back(self, db):
        row = reminders.add(db, "pay the licence", TODAY)

        assert reminders.complete(db, row.id) is not None
        assert reminders.due(db, on=TODAY) == []

    def test_it_is_kept_rather_than_deleted(self, db):
        """ "Did I renew it last year?" is asked afterwards, by the owner or an
        inspector."""
        row = reminders.add(db, "pay the licence", TODAY)
        reminders.complete(db, row.id)

        assert db.get(Reminder, row.id).done_at is not None

    def test_closing_twice_is_not_an_error_and_not_a_second_close(self, db):
        row = reminders.add(db, "pay the licence", TODAY)
        reminders.complete(db, row.id)

        assert reminders.complete(db, row.id) is None

    def test_finding_by_wording(self, db):
        reminders.add(db, "renew the halal certificate", TODAY)
        reminders.add(db, "extinguisher service", TODAY)

        assert [r.what for r in reminders.find(db, "halal")] == ["renew the halal certificate"]

    def test_an_ambiguous_search_returns_everything_it_matched(self, db):
        """So the caller can ask which, rather than close the wrong one."""
        reminders.add(db, "call the supplier about rice", TODAY)
        reminders.add(db, "call the supplier about oil", TODAY)

        assert len(reminders.find(db, "call the supplier")) == 2


class TestChasingWithoutNagging:
    """A reminder repeated verbatim every morning is one the owner learns to
    skip, which is the same as not having it."""

    def test_what_was_raised_today_is_not_raised_again(self, db):
        row = reminders.add(db, "renew the halal certificate", TODAY)
        items = reminders.due(db, on=TODAY)

        reminders.mark_raised(db, [row.id], on=TODAY)

        assert reminders.unraised_today(db, items, on=TODAY) == []

    def test_tomorrow_it_is_raised_again(self, db):
        row = reminders.add(db, "renew the halal certificate", TODAY + timedelta(days=3))
        reminders.mark_raised(db, [row.id], on=TODAY)
        items = reminders.due(db, on=TODAY + timedelta(days=1))

        assert len(reminders.unraised_today(db, items, on=TODAY + timedelta(days=1))) == 1
