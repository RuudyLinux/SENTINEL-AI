import logging

from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.orm import sessionmaker, declarative_base

from .config import settings

logger = logging.getLogger("sentinel.db")


def database_url() -> str:
    """DATABASE_URL if set (production PostgreSQL), otherwise SQLite at
    DB_PATH, which is what dev checkouts and the test suite use."""
    return (settings.database_url or "").strip() or f"sqlite:///{settings.db_path}"


DATABASE_URL = database_url()
IS_SQLITE = DATABASE_URL.startswith("sqlite")


# SQLite's busy-wait before "database is locked". The DBAPI timeout, the
# PRAGMA below and db_retry.close_session (which has to wait at least this
# long for an in-flight commit) all use this.
SQLITE_BUSY_TIMEOUT_SECONDS = 30


def _engine_kwargs() -> dict:
    if IS_SQLITE:
        return {
            # Python's default 5s was too short with 2+ camera workers
            # committing every frame: SQLite gave up before our retry ran
            "connect_args": {"check_same_thread": False, "timeout": SQLITE_BUSY_TIMEOUT_SECONDS},
            # Each running camera worker holds one Session, one connection,
            # for its whole stream. Unset, SQLite got QueuePool's default
            # 5 + 10 and bulk-starting cameras crashed workers with
            # "QueuePool limit of size 5 overflow 10 reached". Same two
            # settings as PostgreSQL; a held SQLite connection is just a
            # file handle, so sizing it generously costs nothing.
            "pool_size": settings.db_pool_size,
            "max_overflow": settings.db_max_overflow,
        }
    # PostgreSQL: long-lived worker sessions plus request ones, so the pool
    # has to be bigger than the camera cap
    return {
        "pool_size": settings.db_pool_size,
        "max_overflow": settings.db_max_overflow,
        # Worker sessions live as long as the stream and will outlive a
        # server idle timeout or NAT rebalance; pre_ping makes that a quiet
        # reconnect instead of a worker crash. Pointless for a local SQLite file.
        "pool_pre_ping": True,
        "pool_recycle": settings.db_pool_recycle_seconds,
    }


engine = create_engine(DATABASE_URL, **_engine_kwargs())


@event.listens_for(engine, "connect")
def _set_sqlite_pragmas(dbapi_connection, _record):
    """WAL lets readers go on while one writer writes (the default journal
    blocks everyone), the usual fix for lots of short transactions from many
    connections, which is exactly one-task-per-camera. busy_timeout matches
    connect_args["timeout"].

    SQLite only; these PRAGMAs are a syntax error on PostgreSQL, which gets
    non-blocking readers from MVCC anyway.
    """
    if not IS_SQLITE:
        return
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute(f"PRAGMA busy_timeout={SQLITE_BUSY_TIMEOUT_SECONDS * 1000}")
    # SQLite ignores foreign keys unless this is on, per connection, while
    # the PostgreSQL schema always enforced them. Without it a violation
    # passed in dev and raised in production, and no SQLite test could see
    # it: deleting a camera left a detection, alert, incident, evidence row,
    # plate and zone pointing at nothing, with a 200.
    #
    # conftest's client fixture had to stop bulk-deleting cameras first (143
    # errors + 5 failures). Fixed the fixture, got green with FKs off, then
    # turned this on and got green again.
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()


SessionLocal = sessionmaker(
    autocommit=False,
    autoflush=False,
    # expire_on_commit=True expires every object after each commit, so the
    # next plain attribute read (camera.fps) quietly SELECTs again. Worker
    # sessions commit almost every frame and read the same camera object
    # thousands of times; one of those implicit reloads hitting a lock was
    # killing workers from lines with no query in sight. Turning it off
    # removes that whole class. Request sessions are short-lived and call
    # db.refresh() when they need a fresh read (create_camera does).
    expire_on_commit=False,
    bind=engine,
)
Base = declarative_base()


# LIKE/ILIKE patterns. % and _ are wildcards, so pasted text becomes syntax:
# /api/search?q=% returned all 30 cameras and GJ_5 matched GJ05. A search that
# quietly widens itself is worse than one that finds nothing, the extra rows
# look like findings. Four endpoints build patterns (global search, audit
# actor/action, self-heal messages), so the helper is here. Each call site
# passes escape=LIKE_ESCAPE; SQLite and PostgreSQL both honour it.
LIKE_ESCAPE = "\\"


def like_pattern(text: str) -> str:
    """A contains-match pattern in which `text` is matched LITERALLY."""
    escaped = (
        text.replace(LIKE_ESCAPE, LIKE_ESCAPE * 2)
        .replace("%", LIKE_ESCAPE + "%")
        .replace("_", LIKE_ESCAPE + "_")
    )
    return f"%{escaped}%"


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def ensure_columns(table: str, columns: dict[str, str], backfill_defaults: dict[str, str] | None = None) -> list[str]:
    """Additive column migration for SQLite.

    create_all() never alters an existing table, so for each name: sql_type
    missing from `table` this runs ALTER TABLE ... ADD COLUMN.

    backfill_defaults (name -> SQL literal like "''" or "0") fills existing
    rows for columns the model gives a non-null default; ADD COLUMN can't
    apply it retroactively and NULLs break response validation. Optional
    columns just stay NULL.

    Idempotent, never drops or renames. ADD COLUMN can't carry UNIQUE or a
    PK, so uniqueness on a migrated column (external_catalog_id) is enforced
    by the app on databases that predate it.
    """
    if not IS_SQLITE:
        # SQLite DDL only (DATETIME isn't a PostgreSQL type). Elsewhere
        # Alembic owns the schema and `alembic upgrade head` is the deploy step.
        logger.debug("ensure_columns(%s) skipped — Alembic owns the schema on %s", table, engine.dialect.name)
        return []
    added: list[str] = []
    inspector = inspect(engine)
    if table not in inspector.get_table_names():
        return added  # no table yet, create_all() makes it with the column
    existing = {c["name"] for c in inspector.get_columns(table)}
    with engine.begin() as conn:
        for name, ddl_type in columns.items():
            if name not in existing:
                # bandit B608: table/name/ddl_type are hardcoded literals from
                # main.py, never user input, and identifiers can't be bound anyway
                conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {ddl_type}"))  # nosec B608
                added.append(name)
        for name, default_sql in (backfill_defaults or {}).items():
            if name in columns:  # only touch columns this call actually manages
                # default_sql is a hardcoded literal too
                conn.execute(text(f"UPDATE {table} SET {name} = {default_sql} WHERE {name} IS NULL"))  # nosec B608
    return added


def ensure_indexes(table: str, index_columns: "list[str | tuple[str, ...]]") -> list[str]:
    """Additive index migration, like ensure_columns. index=True only applies
    when create_all() makes the table, so existing DBs need these.
    ix_{table}_{column} per name, CREATE INDEX IF NOT EXISTS, safe every
    startup. A tuple is one composite index, ix_{table}_{a}_{b}."""
    if not IS_SQLITE:
        logger.debug("ensure_indexes(%s) skipped — Alembic owns the schema on %s", table, engine.dialect.name)
        return []
    created: list[str] = []
    inspector = inspect(engine)
    if table not in inspector.get_table_names():
        return created  # no table yet, create_all() makes the index too
    with engine.begin() as conn:
        for column in index_columns:
            columns = (column,) if isinstance(column, str) else tuple(column)
            name = f"ix_{table}_{'_'.join(columns)}"
            column = ", ".join(columns)
            # bandit B608: same as ensure_columns, hardcoded literals only
            conn.execute(text(f"CREATE INDEX IF NOT EXISTS {name} ON {table} ({column})"))  # nosec B608
            created.append(name)
    return created
