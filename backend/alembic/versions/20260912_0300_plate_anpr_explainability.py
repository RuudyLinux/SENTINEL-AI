"""ANPR explainability columns on plates.

Why a sighting was believed: winning preprocessing variant, variants
agreeing, corroborated across frames, and the saved plate crop. Four columns,
not a blended score; plates.confidence stays the OCR engine's number
(anpr.OcrRead, plate_tracker.Consensus).

Nullable, no backfill. corroborated especially isn't set true on old rows,
they came from a pipeline that persisted on the first passing read. NULL =
unknown.

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
