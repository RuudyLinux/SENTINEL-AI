"""Vehicle.plate_corroborated — A1 precision hardening.

Records whether a vehicle's plate has ever been corroborated across frames, so a
watchlist match can require BOTH a confident read and multi-frame agreement
before escalating to CRITICAL.

Why a separate column rather than deriving it from confidence: measured on the
labelled benchmark (docs/ANPR_ACCURACY.md, "A1"), OCR confidence does NOT
separate correct reads from wrong ones. Correct reads span 0.262-0.990; wrong
plate-shaped reads span 0.260-0.956, and six of seven wrong reads sit at or
above the lowest correct read's confidence. Corroboration is independent
evidence, and blending the two would destroy exactly the distinction this column
exists to preserve.

NULLABLE with NO backfill, deliberately. Existing rows were written by a
pipeline that escalated on confidence alone; marking them corroborated would
assert evidence that was never gathered. NULL reads as "not corroborated", which
caps those vehicles' watchlist alerts at HIGH until a fresh corroborated
sighting arrives — the safe direction for a missing safety signal.

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
