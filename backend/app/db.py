from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.orm import sessionmaker, declarative_base

from .config import settings

engine = create_engine(
    f"sqlite:///{settings.db_path}",
    # `timeout` is sqlite3's own busy-wait budget before raising "database is
    # locked" — Python's 5s default was too short once 2+ concurrent camera
    # workers commit every frame (confirmed in Phase 4: a "database is
    # locked" mid-flush killed a worker task even with retry logic, because
    # SQLite gave up waiting for the lock before the retry ever ran).
    connect_args={"check_same_thread": False, "timeout": 30},
)


@event.listens_for(engine, "connect")
def _set_sqlite_pragmas(dbapi_connection, _record):
    """WAL mode lets readers proceed without blocking on the single writer
    (SQLite's default rollback-journal mode blocks everyone during a write)
    — the standard fix for "many short transactions from concurrent
    connections" in one SQLite file, which is exactly this project's
    per-frame-commit, one-task-per-camera pattern. `busy_timeout` is the
    same budget as `connect_args["timeout"]` above, set at the SQLite level
    too so it applies uniformly regardless of driver default."""
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA busy_timeout=30000")
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
