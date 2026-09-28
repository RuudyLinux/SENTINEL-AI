"""cameras.retired: take a camera out of service without deleting history.

A camera with history can't be deleted (409), and there was no other way to
retire one: disconnecting didn't persist and startup restarted every
video_file camera.

NOT NULL default false; every existing camera is active.

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
