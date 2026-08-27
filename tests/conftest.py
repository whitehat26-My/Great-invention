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
#
# Forced rather than defaulted, and the keys are blanked with it. `setdefault`
# yields to an exported LLM_PROVIDER, and a developer with real credentials in
# their .env would then have the suite quietly making live calls and billing
# them for it. Tests that want a provider set one themselves, with a key that
# is not real.
os.environ["LLM_PROVIDER"] = "fake"
os.environ["ANTHROPIC_API_KEY"] = ""
os.environ["GOOGLE_API_KEY"] = ""


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
def db(engine: Engine, seeded: None, monkeypatch: pytest.MonkeyPatch) -> Iterator[Session]:
    """A session in a transaction that is always rolled back.

    The application's own ``session_scope()`` is redirected onto this same
    connection for the duration of the test. Without that, an agent under test
    opens its own session against the global engine, commits outside the test's
    transaction, and leaves purchase orders and stock movements behind — which
    then change what the *next* test sees. That is not hypothetical: it silently
    turned the purchase-order approval tests into skips, because a previous
    test's committed orders counted as stock already on order and pushed every
    ingredient back above its reorder point.

    ``join_transaction_mode="create_savepoint"`` means a commit inside the
    application code releases a savepoint rather than ending the outer
    transaction, so the rollback below still undoes everything.
    """
    connection = engine.connect()
    transaction = connection.begin()

    import restaurant_ai.db.base as db_base

    bound = sessionmaker(
        bind=connection, expire_on_commit=False, join_transaction_mode="create_savepoint"
    )
    monkeypatch.setattr(db_base, "get_sessionmaker", lambda: bound)

    session = bound()
    try:
        yield session
    finally:
        session.close()
        transaction.rollback()
        connection.close()


@pytest.fixture
def stock_is_low(db) -> None:
    """Guarantee at least one ingredient sits below its reorder point.

    The purchase-order approval tests used to depend on whatever the database
    happened to hold, and skipped when it held enough stock. A test that
    silently stops running is not protecting anything — and these cover the gate
    between an agent proposing a purchase and money being spent.

    Drains the chicken to almost nothing and pins a reorder policy that
    guarantees a trigger, inside the test transaction.
    """
    from decimal import Decimal

    from sqlalchemy import func, select

    from restaurant_ai import clock
    from restaurant_ai.db.models import (
        Ingredient,
        MovementReason,
        PurchaseOrder,
        PurchaseOrderStatus,
        ReorderPolicy,
        StockMovement,
    )

    # Ignore inbound stock from any order already in flight.
    db.execute(
        PurchaseOrder.__table__.update()
        .where(
            PurchaseOrder.status.in_(
                [
                    PurchaseOrderStatus.APPROVED,
                    PurchaseOrderStatus.SENT,
                    PurchaseOrderStatus.PARTIALLY_RECEIVED,
                ]
            )
        )
        .values(status=PurchaseOrderStatus.RECEIVED)
    )

    ingredient = db.execute(
        select(Ingredient).where(Ingredient.code == "ING-CHKN-THI")
    ).scalar_one()

    on_hand = Decimal(
        str(
            db.execute(
                select(func.coalesce(func.sum(StockMovement.quantity), 0)).where(
                    StockMovement.ingredient_id == ingredient.id
                )
            ).scalar_one()
        )
    )
    # Leave a token amount so it reads as "nearly out", not "never stocked".
    if on_hand > Decimal("100"):
        db.add(
            StockMovement(
                ingredient_id=ingredient.id,
                quantity=-(on_hand - Decimal("100")),
                reason=MovementReason.COUNT_ADJUSTMENT,
                unit_cost=ingredient.cost_per_base_unit,
                occurred_at=clock.now(),
                source_type="test_fixture",
                source_id="stock_is_low",
                note="Drained by the stock_is_low fixture",
            )
        )

    policy = db.execute(
        select(ReorderPolicy).where(ReorderPolicy.ingredient_id == ingredient.id)
    ).scalar_one_or_none()
    if policy is None:
        policy = ReorderPolicy(ingredient_id=ingredient.id)
        db.add(policy)
    policy.avg_daily_usage = Decimal("2000")
    policy.usage_stddev = Decimal("250")
    policy.reorder_point = Decimal("5000")
    policy.safety_stock = Decimal("500")
    policy.target_days_cover = 7
    db.flush()
