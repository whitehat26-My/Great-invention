"""Henry is told who works here in a message, not in a spreadsheet.

The owner knows a name and a job. Everything else — the days, the hours, the
caps — is Henry's to assume and theirs to correct: "Ahmad cannot work Fridays"
is how a shift change actually reaches anyone in a place this size.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from restaurant_ai.agents.workforce.shift_scheduling import hire, set_availability
from restaurant_ai.db.models import Staff
from restaurant_ai.kernel.spec import ToolContext

pytestmark = pytest.mark.db


@pytest.fixture
def desk(db):
    from restaurant_ai import clock

    return ToolContext(
        session=db, run_id="test", agent_name="shift_scheduling", business_date=clock.today()
    )


@pytest.fixture(autouse=True)
def _only_ours(db):
    """The seeded restaurant has staff; these tests are about the ones they add."""
    yield
    db.rollback()


class TestHiringFromAMessage:
    def test_a_name_and_a_role_is_enough(self, desk, db):
        result = hire(desk, name="Azman Bin Ali", role="server")

        assert result["hired"]
        person = db.execute(select(Staff).where(Staff.name == "Azman Bin Ali")).scalar_one()
        assert person.role == "server"
        assert len(person.availability) == 7, "available every day until told otherwise"

    def test_no_wage_is_invented(self, desk, db):
        """A made-up rate becomes the labour cost in Camelia's report, and nobody
        would know where it came from."""
        hire(desk, name="Wage Test Person", role="line_cook")

        person = db.execute(select(Staff).where(Staff.name == "Wage Test Person")).scalar_one()
        assert person.hourly_rate == 0
        assert "Wage not set" in hire(desk, name="Another One", role="server")["note"]

    def test_a_job_that_is_not_a_job_lists_the_ones_that_are(self, desk):
        result = hire(desk, name="Someone", role="chief cook")

        assert not result["hired"]
        assert "server" in result["roles"] and "line_cook" in result["roles"]

    def test_hiring_the_same_person_twice_is_refused(self, desk):
        hire(desk, name="Only Once", role="host")
        again = hire(desk, name="Only Once", role="host")

        assert not again["hired"]
        assert "already on the books" in again["note"]


class TestChangingWhenSomeoneCanWork:
    def test_cannot_work_fridays(self, desk, db):
        hire(desk, name="Friday Person", role="server")

        result = set_availability(desk, who="Friday Person", days="0,1,2,3,5,6")

        assert result["changed"]
        assert "Fri" not in result["days"]
        person = db.execute(select(Staff).where(Staff.name == "Friday Person")).scalar_one()
        assert sorted(a.weekday for a in person.availability) == [0, 1, 2, 3, 5, 6]

    def test_hours_can_be_narrowed(self, desk, db):
        hire(desk, name="Mornings Only", role="barista")

        set_availability(desk, who="Mornings Only", days="0,1,2", start="07:00", end="14:00")

        person = db.execute(select(Staff).where(Staff.name == "Mornings Only")).scalar_one()
        assert str(person.availability[0].start_time) == "07:00:00"
        assert str(person.availability[0].end_time) == "14:00:00"

    def test_an_ambiguous_name_asks_rather_than_guesses(self, desk):
        """Changing the wrong person's days rosters somebody who cannot come and
        leaves somebody who can at home. Neither shows up as an error."""
        hire(desk, name="Ahmad Bin Salleh", role="server")
        hire(desk, name="Ahmad Bin Yusof", role="host")

        result = set_availability(desk, who="Ahmad", days="0,1")

        assert not result["changed"]
        assert len(result["candidates"]) == 2

    def test_a_day_that_is_not_a_day_is_refused(self, desk):
        hire(desk, name="Bad Day Person", role="server")

        result = set_availability(desk, who="Bad Day Person", days="0,9")

        assert not result["changed"]
        assert "0-6" in result["note"]

    def test_changing_twice_replaces_rather_than_accumulates(self, desk, db):
        hire(desk, name="Twice Changed", role="server")
        set_availability(desk, who="Twice Changed", days="0,1,2,3,4")
        set_availability(desk, who="Twice Changed", days="5,6")

        person = db.execute(select(Staff).where(Staff.name == "Twice Changed")).scalar_one()
        assert sorted(a.weekday for a in person.availability) == [5, 6]
