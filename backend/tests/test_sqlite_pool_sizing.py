"""SQLite engine gets a real connection pool.

Each camera worker holds one Session, one connection, for its whole stream.
The SQLite branch of db._engine_kwargs() set no pool size, so QueuePool's
default 5 + 10 applied, and bulk-starting cameras gave:

    sqlalchemy.exc.TimeoutError: QueuePool limit of size 5 overflow 10
    reached, connection timed out, timeout 30.00

SQLite is the default for local and demo runs.
"""
import pytest
from sqlalchemy.pool import QueuePool

from app.config import settings
from app.db import engine, IS_SQLITE, SessionLocal


def test_the_running_engine_is_sqlite_in_this_test_suite():
    """Only meaningful on SQLite; Postgres was already sized."""
    assert IS_SQLITE, "expected the test suite's default SQLite engine"


def test_the_pool_is_sized_from_settings_not_left_at_sqlalchemy_defaults():
    pool = engine.pool
    assert isinstance(pool, QueuePool), "expected QueuePool (the pool class that has this limit at all)"
    assert pool.size() == settings.db_pool_size
    assert pool._max_overflow == settings.db_max_overflow
    # SQLAlchemy's defaults when nothing is set, which SQLite used to get
    assert not (pool.size() == 5 and pool._max_overflow == 10), (
        "pool is sized at SQLAlchemy's raw defaults (5+10=15) — the SQLite "
        "branch of db._engine_kwargs() has stopped setting pool_size/max_overflow"
    )


def test_more_sessions_than_the_old_default_pool_can_be_held_open_at_once():
    """20 long-lived Sessions at once, like 20 workers, more than the old
    15 total. Every checkout must succeed."""
    held = []
    try:
        for _ in range(20):
            session = SessionLocal()
            # actually check out a connection, a Session doesn't until it
            # executes something
            session.execute(__import__("sqlalchemy").text("SELECT 1"))
            held.append(session)
    except Exception as exc:  # pragma: no cover - failure path under test
        pytest.fail(
            f"could not hold {len(held) + 1} concurrent sessions open "
            f"(pool_size={settings.db_pool_size}, max_overflow={settings.db_max_overflow}): {exc}"
        )
    finally:
        for session in held:
            session.close()

    assert len(held) == 20
