"""Queries have to run on PostgreSQL, not just SQLite.

events-by-hour grouped by func.strftime, which SQLAlchemy passes straight
through and PostgreSQL doesn't have:

    (psycopg.errors.UndefinedFunction) function strftime(unknown, timestamp
    without time zone) does not exist

so the dashboard chart 500'd in production while passing here. The live
check is tools/postgres_verify.py; these are the cheap half: buckets are
right and no SQLite-only function sneaks back in.
"""
import pathlib
import re
import uuid
from datetime import datetime, timedelta

import pytest

from app import models

# SQLite-only functions. Not datetime(): Python's datetime is everywhere and
# extract/func.now cover SQL
_SQLITE_ONLY_SQL = re.compile(r"func\.(strftime|julianday|unixepoch|sqlite_version)\b")

_APP_ROOT = pathlib.Path(__file__).resolve().parent.parent / "app"


@pytest.fixture
def auth(admin_token):
    return {"Authorization": f"Bearer {admin_token}"}


class TestNoSqliteOnlySql:
    def test_no_app_module_calls_a_sqlite_only_sql_function(self):
        offenders = [
            f"{path.relative_to(_APP_ROOT.parent)}:{i}"
            for path in _APP_ROOT.rglob("*.py")
            for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1)
            if _SQLITE_ONLY_SQL.search(line)
        ]
        assert offenders == [], (
            "these compile on SQLite and fail at runtime on PostgreSQL: " + ", ".join(offenders)
        )


class TestEventsByHour:
    def test_detections_are_bucketed_by_hour(self, client, auth, db_session):
        camera = models.Camera(
            camera_code=f"HRS-{uuid.uuid4().hex[:8]}", name="hour bucket cam",
            source_type="mock_vms", source_uri="",
        )
        db_session.add(camera)
        db_session.flush()
        # Two in one hour, one in another, all inside 24h. Minute pinned: with
        # a bare utcnow() - 3h the +20min stamp crossed into the next hour
        # whenever the minute was >= 40, failing about a third of runs.
        base = (datetime.utcnow() - timedelta(hours=3)).replace(minute=5, second=0, microsecond=0)
        stamps = [base, base + timedelta(minutes=20), base + timedelta(hours=1)]
        for when in stamps:
            db_session.add(models.Detection(
                camera_id=camera.id, cls="car", confidence=0.9, bbox=[1, 2, 3, 4], timestamp=when,
            ))
        db_session.commit()

        buckets = {row["hour"]: row["count"] for row in client.get("/api/analytics/events-by-hour", headers=auth).json()}

        first = base.strftime("%Y-%m-%d %H:00")
        second = (base + timedelta(hours=1)).strftime("%Y-%m-%d %H:00")
        assert buckets.get(first, 0) >= 2
        assert buckets.get(second, 0) >= 1

    def test_the_label_format_is_unchanged(self, client, auth):
        """The chart renders this string; building it in Python instead of
        SQL mustn't change its format."""
        for row in client.get("/api/analytics/events-by-hour", headers=auth).json():
            assert re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{2}:00", row["hour"]), row["hour"]

    def test_older_detections_are_excluded(self, client, auth, db_session):
        camera = models.Camera(
            camera_code=f"HRS-{uuid.uuid4().hex[:8]}", name="stale hour cam",
            source_type="mock_vms", source_uri="",
        )
        db_session.add(camera)
        db_session.flush()
        old = datetime.utcnow() - timedelta(days=3)
        db_session.add(models.Detection(
            camera_id=camera.id, cls="car", confidence=0.9, bbox=[1, 2, 3, 4], timestamp=old,
        ))
        db_session.commit()

        buckets = {row["hour"] for row in client.get("/api/analytics/events-by-hour", headers=auth).json()}
        assert old.strftime("%Y-%m-%d %H:00") not in buckets
