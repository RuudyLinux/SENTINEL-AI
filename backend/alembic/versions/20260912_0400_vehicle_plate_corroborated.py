"""vehicles.plate_corroborated.

Whether a vehicle's plate was ever corroborated across frames, so a
watchlist match needs a confident read AND multi-frame agreement for
CRITICAL. Its own column because confidence doesn't separate right from
wrong reads on the benchmark (docs/ANPR_ACCURACY.md, "A1": correct
0.262-0.990, wrong 0.260-0.956).

Nullable, no backfill. Old rows escalated on confidence alone; NULL = not
corroborated, which caps their watchlist alerts at HIGH until a fresh
corroborated sighting.

Revision ID: 7c4a1f0b9e23
Revises: 3b1e7c9d4a02
Create Date: 2026-09-12 04:00:00.000000
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = '7c4a1f0b9e23'
down_revision: Union[str, None] = '3b1e7c9d4a02'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('vehicles', sa.Column('plate_corroborated', sa.Boolean(), nullable=True))


def downgrade() -> None:
    op.drop_column('vehicles', 'plate_corroborated')
