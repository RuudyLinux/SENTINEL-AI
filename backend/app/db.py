import logging

from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.orm import sessionmaker, declarative_base

from .config import settings

logger = logging.getLogger("sentinel.db")


def database_url() -> str:
    """DATABASE_URL if set (PostgreSQL), otherwise SQLite at DB_PATH."""
    return (settings.database_url or "").strip() or f"sqlite:///{settings.db_path}"


DATABASE_URL = database_url()
IS_SQLITE = DATABASE_URL.startswith("sqlite")


# SQLite's busy wait before "database is locked". Shared by the DBAPI timeout,
# the PRAGMA below and db_retry.close_session.
SQLITE_BUSY_TIMEOUT_SECONDS = 30


def _engine_kwargs() -> dict:
    if IS_SQLITE:
        return {
            # Python's default 5s was too short with 2+ camera workers
            # committing every frame: SQLite gave up before our retry ran
            "connect_args": {"check_same_thread": False, "timeout": SQLITE_BUSY_TIMEOUT_SECONDS},
            # Each running camera worker holds one connection for its stream,
            # so SQLite needs the same pool sizing as PostgreSQL.
            "pool_size": settings.db_pool_size,
            "max_overflow": settings.db_max_overflow,
        }
    # PostgreSQL: long-lived worker sessions plus request ones, so the pool
    # has to be bigger than the camera cap
    return {
        "pool_size": settings.db_pool_size,
        "max_overflow": settings.db_max_overflow,
        # Worker sessions outlive server idle timeouts; pre_ping turns that
        # into a quiet reconnect.
        "pool_pre_ping": True,
        "pool_recycle": settings.db_pool_recycle_seconds,
    }


engine = create_engine(DATABASE_URL, **_engine_kwargs())


@event.listens_for(engine, "connect")
def _set_sqlite_pragmas(dbapi_connection, _record):
    """SQLite connection settings. WAL lets readers proceed while one writer
    writes; busy_timeout matches connect_args["timeout"]. Not applied on
    PostgreSQL.
    """
    if not IS_SQLITE:
        return
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute(f"PRAGMA busy_timeout={SQLITE_BUSY_TIMEOUT_SECONDS * 1000}")
    # SQLite ignores foreign keys unless enabled per connection; PostgreSQL
    # always enforces them, so both backends behave the same.
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()


SessionLocal = sessionmaker(
    autocommit=False,
    autoflush=False,
    # Worker sessions commit constantly and keep reading the same objects;
    # expiring on commit would turn each attribute read into a hidden SELECT
    # that can hit a lock. Request sessions refresh explicitly when needed.
    expire_on_commit=False,
    bind=engine,
)
Base = declarative_base()


# LIKE/ILIKE patterns match user text literally: % and _ are escaped so a
# search never silently widens. Call sites pass escape=LIKE_ESCAPE.
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
    """Additive column migration for SQLite databases created before a column
    existed (create_all never alters tables).

    backfill_defaults maps a column to an SQL literal for existing rows, where
    the model has a non-null default. Idempotent; never drops or renames. ADD
    COLUMN can't add UNIQUE, so uniqueness on migrated columns is enforced by
    the application.
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
    """Additive index migration, like ensure_columns: CREATE INDEX IF NOT EXISTS
    ix_{table}_{column}, or ix_{table}_{a}_{b} for a tuple (composite index)."""
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
