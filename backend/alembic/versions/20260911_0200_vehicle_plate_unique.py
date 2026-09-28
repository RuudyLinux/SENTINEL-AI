"""Unique vehicles.plate_text, closing the concurrent-insert race (see
models.Vehicle.plate_text and correlate.upsert_vehicle_for_plate).

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
    # Fails if duplicate plate_text rows already exist, i.e. the race already
    # happened there. Merge those by hand (move Plate/Alert/Incident refs to
    # one survivor) first; picking a winner automatically is exactly the kind
    # of silent identity decision this is meant to prevent.
    #
    # Swaps the baseline's plain index for a unique one, which is what the
    # model renders to. create_unique_constraint failed on SQLite ("No
    # support for ALTER of constraints") and left `alembic check` drifting.
    op.drop_index('ix_vehicles_plate_text', table_name='vehicles')
    op.create_index('ix_vehicles_plate_text', 'vehicles', ['plate_text'], unique=True)


def downgrade() -> None:
    op.drop_index('ix_vehicles_plate_text', table_name='vehicles')
    op.create_index('ix_vehicles_plate_text', 'vehicles', ['plate_text'], unique=False)
