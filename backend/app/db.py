import logging

from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.orm import sessionmaker, declarative_base

from .config import settings

logger = logging.getLogger("sentinel.db")


def database_url() -> str:
    """The datastore this process talks to.

    `DATABASE_URL` wins when set (production: PostgreSQL). With it unset the URL
    is derived from `DB_PATH` exactly as before, so an existing developer
    checkout, and the whole test suite, keep running on SQLite with no config
    change at all. Development on SQLite and production on PostgreSQL is the
    supported split; nothing here forces one on the other.
    """
    return (settings.database_url or "").strip() or f"sqlite:///{settings.db_path}"


DATABASE_URL = database_url()
IS_SQLITE = DATABASE_URL.startswith("sqlite")


# SQLite's busy-wait budget before it raises "database is locked". Defined once
# here because two other things must agree with it: the DBAPI `timeout` and the
# PRAGMA below, and — critically — db_retry.close_session, which waits for an
# in-flight commit and must therefore allow at least this long, since a
# contended commit can legitimately block for the whole budget.
SQLITE_BUSY_TIMEOUT_SECONDS = 30


def _engine_kwargs() -> dict:
    if IS_SQLITE:
        return {
            # `timeout` is sqlite3's own busy-wait budget before raising
            # "database is locked" — Python's 5s default was too short once 2+
            # concurrent camera workers commit every frame (confirmed in Phase
            # 4: a "database is locked" mid-flush killed a worker task even with
            # retry logic, because SQLite gave up waiting for the lock before
            # the retry ever ran).
            "connect_args": {"check_same_thread": False, "timeout": SQLITE_BUSY_TIMEOUT_SECONDS},
            # Real bug this fixes: every running camera worker holds ONE
            # SQLAlchemy Session — one pooled connection — for the ENTIRE
            # lifetime of its stream (see pipeline/worker.py's
            # `db = SessionLocal()` at the top of `_camera_loop`), not just for
            # the length of one query. Left unset here, SQLAlchemy silently
            # applied its QueuePool DEFAULT (size=5, max_overflow=10 -> 15
            # total) — this codebase's own architecture guarantees that limit
            # gets exhausted the moment more than ~15 cameras are running
            # concurrently (confirmed live: bulk-starting cameras via
            # POST /api/cameras/bulk crashed multiple camera workers with
            # `sqlalchemy.exc.TimeoutError: QueuePool limit of size 5 overflow
            # 10 reached`). The PostgreSQL branch below already reasoned about
            # this exact requirement ("must comfortably exceed the camera
            # concurrency cap") and sized its pool accordingly; SQLite — the
            # DEFAULT backend for local/dev/demo use — had nothing. Reuses the
            # same db_pool_size/db_max_overflow settings so one pair of knobs
            # covers both backends, rather than inventing SQLite-specific ones.
            # A held SQLite connection is cheap (a local file handle, not a
            # server-side resource), so there is no real cost to sizing this
            # generously.
            "pool_size": settings.db_pool_size,
            "max_overflow": settings.db_max_overflow,
        }
    # PostgreSQL. The workload is N long-lived camera-worker sessions plus
    # request-scoped ones, so the pool must comfortably exceed the camera
    # concurrency cap or a worker will block waiting for a connection.
    return {
        "pool_size": settings.db_pool_size,
        "max_overflow": settings.db_max_overflow,
        # A camera worker's session is held open for the life of the stream, so
        # it WILL outlive an idle-connection timeout on the server or a NAT
        # rebalance. pre_ping turns that into one transparent reconnect instead
        # of a dead connection surfacing as a worker crash. Not applied to
        # SQLite above: there is no server-side idle timeout or network to drop
        # for a local file connection, so the check would be pure overhead.
        "pool_pre_ping": True,
        "pool_recycle": settings.db_pool_recycle_seconds,
    }


engine = create_engine(DATABASE_URL, **_engine_kwargs())


@event.listens_for(engine, "connect")
def _set_sqlite_pragmas(dbapi_connection, _record):
    """WAL mode lets readers proceed without blocking on the single writer
    (SQLite's default rollback-journal mode blocks everyone during a write)
    — the standard fix for "many short transactions from concurrent
    connections" in one SQLite file, which is exactly this project's
    per-frame-commit, one-task-per-camera pattern. `busy_timeout` is the
    same budget as `connect_args["timeout"]` above, set at the SQLite level
    too so it applies uniformly regardless of driver default.

    Guarded by dialect: these are SQLite PRAGMAs and are a syntax error on
    PostgreSQL, so on any other backend this listener does nothing. Neither
    setting has a PostgreSQL equivalent that needs applying — MVCC gives
    non-blocking readers natively, which is the property WAL is bought for here.
    """
    if not IS_SQLITE:
        return
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute(f"PRAGMA busy_timeout={SQLITE_BUSY_TIMEOUT_SECONDS * 1000}")
    # BUG-C fix, second half (final deep-debug pass): SQLite ignores every
    # FOREIGN KEY in this schema unless this is switched on PER CONNECTION —
    # it defaults to OFF — while the Alembic-managed PostgreSQL schema has
    # always enforced them. Without this line a referential violation
    # silently succeeded in dev/demo and raised a ForeignKeyViolation in
    # production: a divergence no test running on SQLite could ever surface.
    #
    # Measured before the fix: deleting a camera left 1 detection, 1 alert,
    # 1 incident, 1 evidence row (with its capture-time digest), 1 plate and
    # 1 zone dangling at a camera_id that no longer existed, and the API
    # returned 200.
    #
    # Enabling it required fixing the test harness first (tests/conftest.py's
    # `client` fixture bulk-deleted Camera rows without clearing the rows
    # referencing them — 143 errors + 5 failures from that one fixture).
    # Sequence deliberately taken in that order: fix the fixture, prove the
    # suite green with FKs still off, THEN flip this on and prove it green
    # again — so a failure at either step is unambiguous about its cause.
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()


SessionLocal = sessionmaker(
    autocommit=False,
    autoflush=False,
    # Phase 4 root-cause fix: SQLAlchemy's default expire_on_commit=True
    # marks every ORM object "expired" after each commit, so the very next
    # plain attribute read (e.g. `camera.fps`) silently issues a fresh
    # SELECT against the DB. Camera worker sessions commit on nearly every
    # frame and touch the same long-lived `camera` object thousands of
    # times per run — one of those implicit reloads hitting SQLite write
    # contention (2+ concurrent camera workers) is what was actually
    # killing a worker task, at a call site with no `db.commit()`/db.query()
    # anywhere near it and therefore no obvious place to guard. Disabling
    # this removes the whole class of unguarded implicit-query call sites at
    # the root instead of chasing each one; request-scoped sessions
    # (get_db()) are unaffected in practice since they're short-lived and
    # any handler that needs a genuinely fresh read after a write already
    # calls db.refresh() explicitly (e.g. routers/cameras.py create_camera).
    expire_on_commit=False,
    bind=engine,
)
Base = declarative_base()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def ensure_columns(table: str, columns: dict[str, str], backfill_defaults: dict[str, str] | None = None) -> list[str]:
    """Lightweight additive migration for SQLite.

    This project has no Alembic/migration framework — `Base.metadata.create_all()`
    only creates tables that don't exist yet; it never alters an existing
    table's schema. Phase 3 adds a handful of small columns to already-shipped
    tables (source_timestamp, catalogue linkage, clip evidence linkage), so
    this covers exactly that: for each `name: sql_type` pair not already
    present on `table`, runs `ALTER TABLE ... ADD COLUMN`.

    `backfill_defaults` (name -> a SQL literal, e.g. `"''"` or `"0"`) backfills
    existing rows' NULLs for columns the ORM model declares a non-Optional
    Python default for (e.g. `catalog_stale = Column(Boolean, default=False)`)
    — `ALTER TABLE ADD COLUMN` has no way to apply that default retroactively
    to rows that already existed, and leaving them NULL breaks response
    validation for any schema that (correctly) types the field as non-Optional.
    Genuinely-optional columns (nullable timestamps, FKs) are simply omitted
    from `backfill_defaults` and stay NULL.

    Idempotent (safe to call every startup) and additive-only — never drops
    or renames a column. SQLite's ALTER TABLE ADD COLUMN cannot carry a
    UNIQUE/PRIMARY KEY constraint, so any uniqueness needed on a migrated
    column (e.g. `external_catalog_id`) is enforced at the application layer
    (a lookup-before-insert), not the database, on databases that already
    existed before this column was added.
    """
    if not IS_SQLITE:
        # These helpers emit SQLite DDL (`DATETIME` is not a PostgreSQL type,
        # and the whole approach predates having a migration tool). On any other
        # backend Alembic owns the schema — see backend/alembic/. Skipping is
        # correct, not a degradation: running `alembic upgrade head` is the
        # documented deployment step there.
        logger.debug("ensure_columns(%s) skipped — Alembic owns the schema on %s", table, engine.dialect.name)
        return []
    added: list[str] = []
    inspector = inspect(engine)
    if table not in inspector.get_table_names():
        return added  # table doesn't exist yet — create_all() will create it
        # with the column already in place; nothing to migrate.
    existing = {c["name"] for c in inspector.get_columns(table)}
    with engine.begin() as conn:
        for name, ddl_type in columns.items():
            if name not in existing:
                # bandit B608 (possible SQL injection via string-built query):
                # `table`/`name`/`ddl_type` here are never request/user input —
                # every call site (main.py startup) passes hardcoded string
                # literals, and SQLAlchemy's `text()`/DBAPI params can't
                # parameterize identifiers (table/column names) anyway, only
                # values. Safe as written; flagged for visibility, not fixed
                # with bind params, because there's nothing to bind.
                conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {ddl_type}"))  # nosec B608
                added.append(name)
        for name, default_sql in (backfill_defaults or {}).items():
            if name in columns:  # only touch columns this call actually manages
                # Same reasoning as above — `default_sql` is also always a
                # hardcoded literal from a call site (e.g. "''", "0"), never
                # external input.
                conn.execute(text(f"UPDATE {table} SET {name} = {default_sql} WHERE {name} IS NULL"))  # nosec B608
    return added


def ensure_indexes(table: str, index_columns: list[str]) -> list[str]:
    """Additive-only index migration, parallel to `ensure_columns` above.
    `Column(..., index=True)` in models.py only takes effect for tables
    `create_all()` creates fresh — it never alters an existing table — so an
    already-existing DB needs these created explicitly. One single-column index
    per name, `ix_{table}_{column}`, `CREATE INDEX IF NOT EXISTS` so it's safe to
    call every startup."""
    if not IS_SQLITE:
        logger.debug("ensure_indexes(%s) skipped — Alembic owns the schema on %s", table, engine.dialect.name)
        return []
    created: list[str] = []
    inspector = inspect(engine)
    if table not in inspector.get_table_names():
        return created  # table doesn't exist yet — create_all() will create the index too
    with engine.begin() as conn:
        for column in index_columns:
            name = f"ix_{table}_{column}"
            # bandit B608: same as ensure_columns above — `table`/`column`
            # are always hardcoded literals from main.py startup, never
            # external input, and there are no values here to bind.
            conn.execute(text(f"CREATE INDEX IF NOT EXISTS {name} ON {table} ({column})"))  # nosec B608
            created.append(name)
    return created
