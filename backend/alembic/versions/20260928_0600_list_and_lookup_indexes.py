"""Indexes for the list sort keys and the per-alert incident lookup.

Chosen from the queries the app actually runs, measured with EXPLAIN QUERY PLAN
on a copy of the real database (2026-09-28):

- detections(camera_id, timestamp): the live camera page's "newest 50 for this
  camera" read every detection that camera had and sorted them (27 ms on 22k
  rows, growing ~780 MB/day at one AI camera). Now 0.12 ms.
- alerts.timestamp, audit_logs.timestamp, evidence.created_at,
  incidents.created_at: the list endpoints' ORDER BY ... DESC LIMIT, which
  otherwise sorts the whole table on every poll.
- incidents.vehicle_id: every CRITICAL alert looks for an open incident on the
  same vehicle; evidence.incident_id: every incident page loads its evidence.

Not added: zones.camera_id and watchlist_entries.identifier. Both tables hold a
handful of rows, where an index is slower to maintain than the scan it saves.

Revision ID: 3f8a2c9d7e14
Revises: 9d2e4b6a1c57
Create Date: 2026-09-28 06:00:00.000000
"""
from typing import Sequence, Union

from alembic import op


revision: str = '3f8a2c9d7e14'
down_revision: Union[str, None] = '9d2e4b6a1c57'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_INDEXES = (
    ('ix_detections_camera_id_timestamp', 'detections', ['camera_id', 'timestamp']),
    ('ix_alerts_timestamp', 'alerts', ['timestamp']),
    ('ix_audit_logs_timestamp', 'audit_logs', ['timestamp']),
    ('ix_evidence_incident_id', 'evidence', ['incident_id']),
    ('ix_evidence_created_at', 'evidence', ['created_at']),
    ('ix_incidents_vehicle_id', 'incidents', ['vehicle_id']),
    ('ix_incidents_created_at', 'incidents', ['created_at']),
)


def upgrade() -> None:
    for name, table, columns in _INDEXES:
        op.create_index(name, table, columns, unique=False)


def downgrade() -> None:
    for name, table, _ in reversed(_INDEXES):
        op.drop_index(name, table_name=table)
