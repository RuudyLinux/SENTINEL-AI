"""Indexes for list sort keys and the per-alert incident lookup.

Picked from the queries the app runs, EXPLAIN QUERY PLAN on a copy of the
real DB (2026-09-28):

- detections(camera_id, timestamp): the live page's newest-50 read scanned
  and sorted all of a camera's detections (27 ms at 22k rows, and the table
  grows ~780 MB/day per AI camera). Now 0.12 ms.
- alerts.timestamp, audit_logs.timestamp, evidence.created_at,
  incidents.created_at: list endpoints' ORDER BY ... DESC LIMIT sorted the
  whole table every poll.
- incidents.vehicle_id (every CRITICAL alert looks for an open incident on
  the vehicle), evidence.incident_id (every incident page).

Skipped zones.camera_id and watchlist_entries.identifier: a handful of rows,
the index costs more than the scan.

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
