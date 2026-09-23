"""ANPR explainability columns on `plates`.

Records WHY a plate sighting was believed, not just what it said: which
preprocessing variant produced the winning OCR read, how many variants agreed
on it, whether the temporal layer corroborated it across frames, and where the
plate crop OCR actually looked at was saved.

These are deliberately four separate columns rather than one blended score.
`plates.confidence` remains the OCR engine's own number and is never adjusted
by any of them — see pipeline/anpr.py::OcrRead and pipeline/plate_tracker.py::
Consensus for why corroboration and confidence must not be folded together.

All four are NULLABLE with NO backfill. In particular `corroborated` is NOT
defaulted to true for existing rows: those were written by a pipeline that
persisted a plate on its FIRST passing read, so asserting they were corroborated
would claim evidence that was never gathered. NULL reads as "unknown", which is
the only true statement available about them.

Revision ID: 3b1e7c9d4a02
Revises: f273294cc229
Create Date: 2026-09-12 03:00:00.000000
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = '3b1e7c9d4a02'
down_revision: Union[str, None] = 'f273294cc229'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('plates', sa.Column('ocr_variant', sa.String(), nullable=True))
    op.add_column('plates', sa.Column('variants_agreeing', sa.Integer(), nullable=True))
    op.add_column('plates', sa.Column('corroborated', sa.Boolean(), nullable=True))
    op.add_column('plates', sa.Column('plate_crop_path', sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column('plates', 'plate_crop_path')
    op.drop_column('plates', 'corroborated')
    op.drop_column('plates', 'variants_agreeing')
    op.drop_column('plates', 'ocr_variant')
