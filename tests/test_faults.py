"""An exception, turned into a sentence the owner can act on.

The screenshot that caused this: a phone-sized Telegram bubble containing a
psycopg UndefinedTable, the full SELECT with all seventeen columns, the LINE 2
caret and a link to the SQLAlchemy docs — where a sentence should have been.
"""

from __future__ import annotations

from restaurant_ai.faults import database_has_no_tables, recognise, short_fault

# The real thing, verbatim in shape, from the owner's chat.
UNDEFINED_TABLE = (
    'ProgrammingError: (psycopg.errors.UndefinedTable) relation "daily_report" '
    "does not exist\n"
    "LINE 2: FROM daily_report\n"
    "             ^\n"
    "[SQL: SELECT daily_report.business_date, daily_report.run_id, "
    "daily_report.net_revenue, daily_report.covers, daily_report.average_check, "
    "daily_report.cogs, daily_report.labour_cost FROM daily_report WHERE "
    "daily_report.business_date = %(business_date_1)s::DATE]\n"
    "[parameters: {'business_date_1': datetime.date(2026, 8, 28)}]\n"
    "(Background on this error at: https://sqlalche.me/e/20/f405)"
)


class TestTheOwnerNeverSeesSql:
    def test_the_statement_and_the_footnotes_are_dropped(self):
        said = short_fault(RuntimeError(UNDEFINED_TABLE))
        assert "SELECT" not in said
        assert "sqlalche.me" not in said
        assert "parameters:" not in said

    def test_it_fits_in_a_phone_bubble(self):
        assert len(short_fault(RuntimeError(UNDEFINED_TABLE))) < 200
        assert len(short_fault(RuntimeError("x" * 5000))) < 200

    def test_an_unknown_failure_still_names_its_type(self):
        """Condensing must not become hiding."""
        said = short_fault(ValueError("something specific went wrong"))
        assert "ValueError" in said
        assert "something specific went wrong" in said


class TestRecognisingWhatActuallyHappens:
    def test_an_empty_schema_names_the_command_that_fixes_it(self):
        said = short_fault(RuntimeError(UNDEFINED_TABLE))
        assert "no tables yet" in said
        assert "alembic upgrade head" in said

    def test_a_cascade_failure_is_not_reported_as_a_second_bug(self):
        """One broken query aborts the transaction; five sections then fail.

        Reporting five faults for one cause sends the owner hunting for four
        problems that do not exist.
        """
        said = short_fault(
            RuntimeError("InternalError: current transaction is aborted, commands ignored")
        )
        assert "earlier query" in said

    def test_a_stopped_database_is_not_an_empty_one(self):
        said = short_fault(RuntimeError("connection refused"))
        assert "not running" in said
        assert "no tables" not in said

    def test_a_wrong_password_points_at_the_setting(self):
        assert "POSTGRES_PASSWORD" in short_fault(
            RuntimeError("password authentication failed for user")
        )

    def test_an_unrecognised_failure_gets_no_invented_fix(self):
        assert recognise(ValueError("who knows")) is None

    def test_the_empty_schema_case_is_detectable_for_migrating(self):
        assert database_has_no_tables(RuntimeError(UNDEFINED_TABLE))
        assert not database_has_no_tables(RuntimeError("connection refused"))
