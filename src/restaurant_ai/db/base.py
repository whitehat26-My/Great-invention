"""Engine, session factory and the declarative base.

One engine per process, created lazily so importing a model never opens a
connection (which matters for Alembic and for the test suite).
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import DateTime, Engine, MetaData, Numeric, String, create_engine, func
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker

from restaurant_ai.config import get_settings

# Explicit naming convention so Alembic autogenerate produces stable, nameable
# constraints instead of database-assigned ones.
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

# Money and quantity precisions used across every table.
Money = Numeric(14, 2)
Qty = Numeric(14, 4)
Pct = Numeric(9, 6)


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)

    def __repr__(self) -> str:  # pragma: no cover - debugging affordance
        pk = getattr(self, "id", None)
        return f"<{type(self).__name__} id={pk}>"


def new_uuid() -> str:
    return str(uuid.uuid4())


def utcnow() -> datetime:
    return datetime.now(tz=UTC)


class UUIDPk:
    """Mixin: string UUID primary key, generated application-side."""

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)


class Timestamped:
    """Mixin: server-side created/updated stamps."""

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


_engine: Engine | None = None
_sessionmaker: sessionmaker[Session] | None = None


def get_engine(url: str | None = None, **kwargs: Any) -> Engine:
    global _engine
    if url is not None:
        return create_engine(url, pool_pre_ping=True, future=True, **kwargs)
    if _engine is None:
        _engine = create_engine(
            get_settings().database_url, pool_pre_ping=True, future=True, **kwargs
        )
    return _engine


def get_sessionmaker() -> sessionmaker[Session]:
    global _sessionmaker
    if _sessionmaker is None:
        _sessionmaker = sessionmaker(bind=get_engine(), expire_on_commit=False, future=True)
    return _sessionmaker


def reset_engine() -> None:
    """Test hook: drop the cached engine/sessionmaker after settings change."""
    global _engine, _sessionmaker
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _sessionmaker = None


@contextmanager
def session_scope(existing: Session | None = None) -> Iterator[Session]:
    """Transactional scope.

    Passing ``existing`` makes this a no-op pass-through, so helpers can be
    composed inside a caller's transaction without nesting commits.
    """
    if existing is not None:
        yield existing
        return
    session = get_sessionmaker()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def money(value: str | int | float | Decimal) -> Decimal:
    """Coerce to 2dp money, the rounding used consistently across the ledger."""
    return Decimal(str(value)).quantize(Decimal("0.01"))


def qty(value: str | int | float | Decimal) -> Decimal:
    return Decimal(str(value)).quantize(Decimal("0.0001"))
