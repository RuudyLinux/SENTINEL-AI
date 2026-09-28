"""Alembic environment.

The URL comes from the app's settings, not alembic.ini, so upgrade can't
hit a different database than the app opens. Importing app.models registers
every table on Base.metadata for autogenerate.
"""
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from app import models  # noqa: F401  (registers every table on Base)
from app.db import Base, database_url

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

config.set_main_option("sqlalchemy.url", database_url().replace("%", "%%"))

target_metadata = Base.metadata

# PostGIS objects not on the ORM on purpose: spatial_ref_sys, and the generated
# cameras.geog column + index (migration 20260928_0700, read by app/geo.py).
# Without this `alembic check` calls them drift.
_NOT_MODEL_OWNED = {("table", "spatial_ref_sys"), ("column", "geog"), ("index", "ix_cameras_geog")}


def include_object(obj, name, type_, reflected, compare_to):
    return (type_, name) not in _NOT_MODEL_OWNED


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        # SQLite can't ALTER most column properties, batch mode does the
        # copy-and-swap. harmless on PostgreSQL, one script for both
        render_as_batch=True,
        include_object=include_object,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        if connection.dialect.name == "postgresql":
            # the PostGIS image also puts topology/tiger schemas on the search
            # path and their tables would show up as drift. ours live in public
            connection.exec_driver_sql("SET search_path TO public")
            connection.commit()
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            render_as_batch=True,
            # compare types too, otherwise a changed column type is silently ignored
            compare_type=True,
            include_object=include_object,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
