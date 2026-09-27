"""Camera.retired — retire a camera without deleting its history.

A camera with detections, alerts, incidents or evidence cannot be deleted (the
API refuses with 409, correctly: deleting it would orphan chain-of-custody
records). Before this column there was no other way to take one out of service:
disconnecting did not persist, and startup restarted every video_file camera.

NOT NULL with a false default, so every existing camera migrates as active —
true of all of them, since nothing could retire one before.

Revision ID: 9d2e4b6a1c57
Revises: 7c4a1f0b9e23
Create Date: 2026-09-27 05:00:00.000000
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = '9d2e4b6a1c57'
down_revision: Union[str, None] = '7c4a1f0b9e23'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('cameras') as batch:
        batch.add_column(sa.Column('retired', sa.Boolean(), nullable=False, server_default=sa.false()))


def downgrade() -> None:
    with op.batch_alter_table('cameras') as batch:
        batch.drop_column('retired')
