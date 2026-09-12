"""10/10 roadmap gap-closure fields: ANPR human review, alert feedback,
evidence provenance, tamper-evident audit chain.

Revision ID: 875ca01ff2a8
Revises: b64151666283
Create Date: 2026-09-10 01:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '875ca01ff2a8'
down_revision: Union[str, None] = 'b64151666283'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # --- Plate: human-in-the-loop ANPR review (P7) ---
    op.add_column('plates', sa.Column('review_status', sa.String(), nullable=True, server_default='auto_accepted'))
    op.add_column('plates', sa.Column('reviewed_by', sa.String(), nullable=True))
    op.add_column('plates', sa.Column('reviewed_at', sa.DateTime(), nullable=True))
    op.add_column('plates', sa.Column('corrected_text', sa.String(), nullable=True))
    # batch_alter_table, not a bare op.create_foreign_key: SQLite cannot ALTER
    # a constraint onto an existing table, so the bare form aborted
    # `alembic upgrade head` with "No support for ALTER of constraints in
    # SQLite dialect". Batch mode recreates the table with the constraint;
    # on PostgreSQL it emits the same plain ALTER as before.
    with op.batch_alter_table('plates', schema=None) as batch_op:
        batch_op.create_foreign_key('fk_plates_reviewed_by_users', 'users', ['reviewed_by'], ['id'])
    op.create_index('ix_plates_review_status', 'plates', ['review_status'])

    # --- Alert: false-positive feedback (P6) ---
    op.add_column('alerts', sa.Column('feedback', sa.String(), nullable=True))
    op.add_column('alerts', sa.Column('feedback_reason', sa.String(), nullable=True))
    op.add_column('alerts', sa.Column('feedback_by', sa.String(), nullable=True))
    op.add_column('alerts', sa.Column('feedback_at', sa.DateTime(), nullable=True))
    with op.batch_alter_table('alerts', schema=None) as batch_op:
        batch_op.create_foreign_key('fk_alerts_feedback_by_users', 'users', ['feedback_by'], ['id'])
    op.create_index('ix_alerts_feedback', 'alerts', ['feedback'])

    # --- Evidence: provenance completion (P8) ---
    op.add_column('evidence', sa.Column('model_version', sa.String(), nullable=True))
    op.add_column('evidence', sa.Column('rule_version', sa.String(), nullable=True))

    # --- AuditLog: tamper-evident hash chain (P9) ---
    op.add_column('audit_logs', sa.Column('chain_seq', sa.Integer(), nullable=True))
    op.add_column('audit_logs', sa.Column('prev_hash', sa.String(), nullable=True))
    op.add_column('audit_logs', sa.Column('entry_hash', sa.String(), nullable=True))
    # Real uniqueness at the database level (unlike SQLite's additive
    # ALTER TABLE ADD COLUMN path in app/db.py::ensure_columns, which cannot
    # carry a UNIQUE constraint onto an already-existing table) — a
    # concurrent chain_seq collision raises here rather than only being
    # caught by app/audit.py's own retry-on-IntegrityError loop.
    # A UNIQUE INDEX, not a unique constraint plus a separate plain index:
    # models.py declares `chain_seq = Column(..., unique=True, index=True)`,
    # which SQLAlchemy renders as exactly one unique index. Creating both a
    # constraint and a non-unique index made `alembic check` report permanent
    # drift (remove_constraint + remove_index + add_index) against a database
    # that had just been migrated to head. It also needed no batch mode here:
    # SQLite supports CREATE UNIQUE INDEX on an existing table, only ALTER of
    # constraints is unsupported.
    op.create_index('ix_audit_logs_chain_seq', 'audit_logs', ['chain_seq'], unique=True)


def downgrade() -> None:
    op.drop_index('ix_audit_logs_chain_seq', table_name='audit_logs')
    op.drop_column('audit_logs', 'entry_hash')
    op.drop_column('audit_logs', 'prev_hash')
    op.drop_column('audit_logs', 'chain_seq')

    op.drop_column('evidence', 'rule_version')
    op.drop_column('evidence', 'model_version')

    op.drop_index('ix_alerts_feedback', table_name='alerts')
    with op.batch_alter_table('alerts', schema=None) as batch_op:
        batch_op.drop_constraint('fk_alerts_feedback_by_users', type_='foreignkey')
    op.drop_column('alerts', 'feedback_at')
    op.drop_column('alerts', 'feedback_by')
    op.drop_column('alerts', 'feedback_reason')
    op.drop_column('alerts', 'feedback')

    op.drop_index('ix_plates_review_status', table_name='plates')
    with op.batch_alter_table('plates', schema=None) as batch_op:
        batch_op.drop_constraint('fk_plates_reviewed_by_users', type_='foreignkey')
    op.drop_column('plates', 'corrected_text')
    op.drop_column('plates', 'reviewed_at')
    op.drop_column('plates', 'reviewed_by')
    op.drop_column('plates', 'review_status')
