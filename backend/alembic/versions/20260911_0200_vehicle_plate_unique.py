"""BUG-1 fix (10/10 debugging pass): unique constraint on vehicles.plate_text
to close a real concurrent-insert race — see models.py::Vehicle.plate_text
and pipeline/correlate.py::upsert_vehicle_for_plate for the full reasoning.

Revision ID: f273294cc229
Revises: 875ca01ff2a8
Create Date: 2026-09-11 02:00:00.000000
"""
from typing import Sequence, Union

from alembic import op


revision: str = 'f273294cc229'
down_revision: Union[str, None] = '875ca01ff2a8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # If this fails on a real deployment, it means duplicate plate_text rows
    # already exist (i.e. BUG-1 already happened at least once in that
    # deployment's history) — that data must be manually reconciled (merge
    # the duplicate Vehicle rows' Plate/Alert/Incident references onto one
    # survivor) BEFORE this migration can apply. Not attempted automatically
    # here: an automatic merge choosing which row "wins" is exactly the kind
    # of silent identity decision this whole fix exists to avoid making
    # without a human looking at it.
    # Replaces the baseline's NON-unique index with a unique one, which is
    # what models.py declares (`plate_text = Column(..., unique=True,
    # index=True)` renders as a single unique index).
    #
    # Originally written as `op.create_unique_constraint(...)`. That aborted
    # `alembic upgrade head` on SQLite outright — "No support for ALTER of
    # constraints in SQLite dialect" — so the migration chain could not be
    # applied there at all, and CI's own "migrations apply and roll back" step
    # could never have passed. It also left `alembic check` reporting drift
    # forever, because a constraint plus a plain index is not what the model
    # asks for. A unique index needs no ALTER and satisfies both.
    op.drop_index('ix_vehicles_plate_text', table_name='vehicles')
    op.create_index('ix_vehicles_plate_text', 'vehicles', ['plate_text'], unique=True)


def downgrade() -> None:
    op.drop_index('ix_vehicles_plate_text', table_name='vehicles')
    op.create_index('ix_vehicles_plate_text', 'vehicles', ['plate_text'], unique=False)
