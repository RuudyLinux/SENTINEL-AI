"""Regression: the SQLite engine must have a real connection pool.

The bug, confirmed live: every running camera worker holds ONE SQLAlchemy
Session — one pooled connection — for the ENTIRE lifetime of its stream (see
`pipeline/worker.py`'s `db = SessionLocal()` at the top of `_camera_loop`), not
just for the length of one query. The SQLite branch of `db._engine_kwargs()`
set no pool size at all, so SQLAlchemy silently applied its QueuePool DEFAULT
(size=5, max_overflow=10 = 15 total). Bulk-starting cameras reproduced this
directly:

    sqlalchemy.exc.TimeoutError: QueuePool limit of size 5 overflow 10
    reached, connection timed out, timeout 30.00

This is the DEFAULT backend for local/dev/demo use — the exact path every
judge-demo run and every local developer session goes through.
"""
import pytest
from sqlalchemy.pool import QueuePool

from app.config import settings
from app.db import engine, IS_SQLITE, SessionLocal


def test_the_running_engine_is_sqlite_in_this_test_suite():
    """Precondition — this test's whole point is meaningless against Postgres,
    where the pool was already correctly sized before this fix."""
    assert IS_SQLITE, "expected the test suite's default SQLite engine"


def test_the_pool_is_sized_from_settings_not_left_at_sqlalchemy_defaults():
    pool = engine.pool
    assert isinstance(pool, QueuePool), "expected QueuePool (the pool class that has this limit at all)"
    assert pool.size() == settings.db_pool_size
    assert pool._max_overflow == settings.db_max_overflow
    # The exact defaults SQLAlchemy applies when nothing is configured — the
    # regression this pins down is that the SQLite branch silently fell back
    # to exactly these.
    assert not (pool.size() == 5 and pool._max_overflow == 10), (
        "pool is sized at SQLAlchemy's raw defaults (5+10=15) — the SQLite "
        "branch of db._engine_kwargs() has stopped setting pool_size/max_overflow"
    )


def test_more_sessions_than_the_old_default_pool_can_be_held_open_at_once():
    """Reproduces the actual failure mode: N long-lived Sessions held
    concurrently, exactly like N running camera workers. 20 exceeds the old
    silent default's total capacity (15); every checkout must succeed."""
    held = []
    try:
        for _ in range(20):
            session = SessionLocal()
            # Force a real connection checkout, matching what _camera_loop's
            # first query does — a Session alone doesn't take a connection
            # from the pool until it actually executes something.
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
