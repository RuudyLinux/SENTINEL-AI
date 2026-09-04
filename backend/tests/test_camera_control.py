"""Camera Control Center bulk endpoint: RBAC enforcement, partial-failure
handling (one bad camera never aborts the batch), duplicate-in-progress
skip, and the one audit-log entry per bulk call."""
import asyncio
import os
import sqlite3
import tempfile
import threading
import time

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from app import models
from app.db import Base
from app.routers import camera_control
from app.security import hash_password, create_access_token


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def operator_user(db_session):
    role = db_session.query(models.Role).filter(models.Role.name == "Control Room Operator").first()
    if role is None:
        role = models.Role(name="Control Room Operator", description="test")
        db_session.add(role)
        db_session.flush()
    user = db_session.query(models.User).filter(models.User.username == "test_operator_bulk").first()
    if user is None:
        user = models.User(
            username="test_operator_bulk", password_hash=hash_password("testpass123"),
            full_name="Test Operator", role_id=role.id,
        )
        db_session.add(user)
        db_session.commit()
        db_session.refresh(user)
    return user


@pytest.fixture
def operator_token(operator_user):
    return create_access_token(operator_user)


@pytest.fixture
def viewer_user(db_session):
    """Investigator: no camera-control permission per seed.py's role list —
    stands in for "Viewer: no control actions" (Part 7)."""
    role = db_session.query(models.Role).filter(models.Role.name == "Investigator").first()
    if role is None:
        role = models.Role(name="Investigator", description="test")
        db_session.add(role)
        db_session.flush()
    user = db_session.query(models.User).filter(models.User.username == "test_viewer_bulk").first()
    if user is None:
        user = models.User(
            username="test_viewer_bulk", password_hash=hash_password("testpass123"),
            full_name="Test Viewer", role_id=role.id,
        )
        db_session.add(user)
        db_session.commit()
        db_session.refresh(user)
    return user


@pytest.fixture
def viewer_token(viewer_user):
    return create_access_token(viewer_user)


def _make_camera(client, admin_token, code: str) -> str:
    resp = client.post(
        "/api/cameras",
        json={"camera_code": code, "name": code, "source_type": "mock_vms", "source_uri": ""},
        headers=_auth(admin_token),
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["id"]


def test_viewer_cannot_run_bulk_action(client, viewer_token):
    resp = client.post("/api/cameras/bulk", json={"action": "stop", "camera_ids": []}, headers=_auth(viewer_token))
    assert resp.status_code == 403


def test_operator_can_run_bulk_action(client, operator_token, admin_token):
    cam_id = _make_camera(client, admin_token, "C-BULK-OP")
    resp = client.post(
        "/api/cameras/bulk", json={"action": "stop", "camera_ids": [cam_id]}, headers=_auth(operator_token),
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["total"] == 1
    assert body["successful"] == 1


def test_bulk_action_on_unknown_camera_is_a_partial_failure_not_a_500(client, admin_token):
    good_id = _make_camera(client, admin_token, "C-BULK-GOOD")
    resp = client.post(
        "/api/cameras/bulk",
        json={"action": "stop", "camera_ids": [good_id, "cam_does_not_exist"]},
        headers=_auth(admin_token),
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["total"] == 2
    assert body["successful"] == 1
    assert body["failed"] == 1
    outcomes = {r["camera_id"]: r["ok"] for r in body["results"]}
    assert outcomes[good_id] is True
    assert outcomes["cam_does_not_exist"] is False


def test_bulk_action_empty_target_list_is_a_client_error(client, admin_token):
    resp = client.post("/api/cameras/bulk", json={"action": "stop", "camera_ids": []}, headers=_auth(admin_token))
    assert resp.status_code == 400


def test_bulk_action_records_one_audit_log_entry(client, admin_token, db_session):
    cam_id = _make_camera(client, admin_token, "C-BULK-AUDIT")
    before = db_session.query(models.AuditLog).filter(models.AuditLog.action == "bulk_stop_cameras").count()
    resp = client.post("/api/cameras/bulk", json={"action": "stop", "camera_ids": [cam_id]}, headers=_auth(admin_token))
    assert resp.status_code == 200
    after = db_session.query(models.AuditLog).filter(models.AuditLog.action == "bulk_stop_cameras").count()
    assert after == before + 1


def test_camera_already_in_progress_is_skipped_not_double_actioned(client, admin_token):
    """Duplicate-click / overlapping-bulk-op guard (Part 4): a camera_id
    already marked in-progress by another in-flight bulk call is reported as
    skipped, never actioned twice."""
    cam_id = _make_camera(client, admin_token, "C-BULK-INFLIGHT")
    camera_control._IN_PROGRESS.add(cam_id)
    try:
        resp = client.post("/api/cameras/bulk", json={"action": "stop", "camera_ids": [cam_id]}, headers=_auth(admin_token))
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["skipped"] == 1
        assert body["successful"] == 0
        assert body["results"][0]["skipped"] is True
    finally:
        camera_control._IN_PROGRESS.discard(cam_id)


def test_disruptive_actions_list_matches_documented_set(client, admin_token):
    resp = client.get("/api/cameras/bulk/disruptive-actions", headers=_auth(admin_token))
    assert resp.status_code == 200
    assert set(resp.json()) == {"restart", "disconnect", "stop"}


def _make_short_timeout_engine(db_path: str):
    """Same PRAGMAs as db.py's real engine, short busy_timeout so a real
    lock surfaces in milliseconds — same harness as test_db_concurrency.py."""
    engine = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False, "timeout": 0.2})

    @event.listens_for(engine, "connect")
    def _pragmas(dbapi_connection, _record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA busy_timeout=200")
        cursor.close()

    return engine


def test_set_ai_retries_through_a_real_sqlite_lock_and_durably_persists():
    """Final-review audit finding regression test: camera_control._set_ai
    used to be a bare `db.commit()`, bypassing this PR's own SQLite-lock
    retry — inconsistent with worker.py under the exact sustained
    contention this PR exists to survive, and riskier here since a bulk
    call runs up to MAX_CONCURRENT of these concurrently. Proves _set_ai now
    retries through a REAL second-connection write lock (not mocked) and the
    reapplied value is durably committed, verified via a fresh connection."""
    tmp_dir = tempfile.mkdtemp(prefix="sentinel_bulk_lock_test_")
    db_path = os.path.join(tmp_dir, "bulk_lock_test.db").replace("\\", "/")

    engine = _make_short_timeout_engine(db_path)
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine, autocommit=False, autoflush=False, expire_on_commit=False)

    db = Session()
    camera = models.Camera(
        camera_code="C-BULK-REAL-LOCK", name="bulk lock test", source_type="video_file",
        source_uri="unused.mp4", status="offline", ai_person=False, ai_vehicle=False, ai_anpr=False,
    )
    db.add(camera)
    db.commit()
    camera_id = camera.id

    lock_hold_seconds = 0.6

    def _hold_lock():
        conn = sqlite3.connect(db_path, timeout=30)
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("UPDATE cameras SET name = name WHERE id = ?", (camera_id,))
        time.sleep(lock_hold_seconds)
        conn.commit()
        conn.close()

    holder = threading.Thread(target=_hold_lock)
    holder.start()
    time.sleep(0.15)

    try:
        ok = asyncio.run(camera_control._set_ai(db, camera, True, "C-BULK-REAL-LOCK"))
        assert ok is True
    finally:
        holder.join()
        db.close()

    verify_db = Session()
    try:
        reloaded = verify_db.query(models.Camera).filter(models.Camera.id == camera_id).first()
        assert reloaded is not None
        assert reloaded.ai_person is True and reloaded.ai_vehicle is True and reloaded.ai_anpr is True
    finally:
        verify_db.close()
    engine.dispose()
