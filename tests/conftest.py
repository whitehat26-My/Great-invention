"""Shared test fixtures.

Tests run against a real PostgreSQL instance rather than SQLite: the schema uses
JSONB, partial constraints and expression indexes that SQLite cannot express, so
an in-memory substitute would test something other than what ships.

Each db test runs inside a transaction that is rolled back afterwards, so the
seeded dataset is loaded once per session and every test sees it pristine.
"""

from __future__ import annotations

import os
from collections.abc import Iterator

import pytest
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.orm import Session, sessionmaker

# The whole suite runs on the deterministic fake model: no API key, no network.
os.environ.setdefault("LLM_PROVIDER", "fake")


def _database_available(url: str) -> bool:
    try:
        engine = create_engine(url, pool_pre_ping=True)
        with engine.connect() as conn:
            conn.execute(text("select 1"))
        engine.dispose()
        return True
    except Exception:
        return False


@pytest.fixture(scope="session")
def engine() -> Iterator[Engine]:
    from restaurant_ai.config import get_settings

    url = get_settings().database_url
    if not _database_available(url):
        pytest.skip(f"PostgreSQL not reachable at {url}; run `make up && make migrate`")
    eng = create_engine(url, pool_pre_ping=True, future=True)
    yield eng
    eng.dispose()


@pytest.fixture(scope="session")
def seeded(engine: Engine) -> None:
    """Ensure the demo restaurant exists once for the whole session."""
    from restaurant_ai.db.seed import seed_all

    maker = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    with maker() as session:
        has_menu = session.execute(text("select count(*) from menu_item")).scalar_one()
        if has_menu == 0:
            seed_all(session, history_days=56)
            session.commit()


@pytest.fixture
def db(engine: Engine, seeded: None) -> Iterator[Session]:
    """A session in a transaction that is always rolled back.

    Tests can write freely — purchase orders, journals, stock movements — and
    the database is unchanged afterwards.
    """
    connection = engine.connect()
    transaction = connection.begin()
    session = Session(
        bind=connection, expire_on_commit=False, join_transaction_mode="create_savepoint"
    )
    try:
        yield session
    finally:
        session.close()
        transaction.rollback()
        connection.close()
