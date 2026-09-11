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
    op.create_unique_constraint('uq_vehicles_plate_text', 'vehicles', ['plate_text'])


def downgrade() -> None:
    op.drop_constraint('uq_vehicles_plate_text', 'vehicles', type_='unique')
