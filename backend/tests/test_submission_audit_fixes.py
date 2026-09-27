"""Regressions for the defects found in the final submission audit.

1. A resource token (evidence file / package / camera stream) was accepted as
   a full session token by every authenticated endpoint and by /ws.
2. The Auditor role — "audit-log and compliance visibility" — could
   acknowledge, dismiss and escalate alerts, open/assign/close incidents, and
   accept/correct/reject plate reads.
3. Disabling a watchlist_plate or zone_entry rule did not stop its alerts.
"""
import asyncio
import uuid
from datetime import datetime

import pytest

from app import models
from app.pipeline import rules_engine
from app.security import create_access_token, create_resource_token, hash_password


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


# --- 1. resource token scope ------------------------------------------------

@pytest.mark.parametrize("resource", ["camera_stream", "evidence_file", "evidence_package"])
def test_resource_token_is_not_a_session_token(client, admin_user, resource):
    token = create_resource_token(resource, "any-id", admin_user, 300)
    assert client.get("/api/auth/me", headers=_auth(token)).status_code == 401
    assert client.get("/api/users", headers=_auth(token)).status_code == 401


def test_resource_token_rejected_on_websocket(client, admin_user):
    from starlette.websockets import WebSocketDisconnect

    token = create_resource_token("camera_stream", "any-id", admin_user, 300)
    with pytest.raises(WebSocketDisconnect) as exc:
        with client.websocket_connect(f"/ws?token={token}") as ws:
            ws.receive_text()
    assert exc.value.code == 4401


def test_session_token_still_works(client, admin_token):
    assert client.get("/api/auth/me", headers=_auth(admin_token)).status_code == 200


# --- 2. auditor is read-only ------------------------------------------------

@pytest.fixture
def auditor_token(db_session):
    role = db_session.query(models.Role).filter(models.Role.name == "Auditor").first()
    user = db_session.query(models.User).filter(models.User.username == "audit_fix_auditor").first()
    if user is None:
        user = models.User(
            username="audit_fix_auditor", password_hash=hash_password("testpass123"),
            full_name="Auditor", role_id=role.id,
        )
        db_session.add(user)
        db_session.commit()
        db_session.refresh(user)
    return create_access_token(user)


@pytest.mark.parametrize("method,path", [
    ("post", "/api/alerts/x/acknowledge"),
    ("post", "/api/alerts/x/escalate"),
    ("post", "/api/alerts/x/dismiss"),
    ("post", "/api/alerts/x/feedback"),
    ("post", "/api/incidents"),
    ("post", "/api/incidents/x/notes"),
    ("post", "/api/incidents/x/assign?assignee_user_id=y"),
    ("post", "/api/incidents/x/close"),
    ("post", "/api/review/x/accept"),
    ("post", "/api/review/x/correct"),
    ("post", "/api/review/x/reject"),
])
def test_auditor_cannot_mutate_operational_records(client, auditor_token, method, path):
    resp = getattr(client, method)(path, json={}, headers=_auth(auditor_token))
    assert resp.status_code == 403, (path, resp.status_code, resp.text)


def test_auditor_can_still_read(client, auditor_token):
    assert client.get("/api/alerts", headers=_auth(auditor_token)).status_code == 200
    assert client.get("/api/incidents", headers=_auth(auditor_token)).status_code == 200
    assert client.get("/api/audit", headers=_auth(auditor_token)).status_code == 200


def test_operator_can_still_act(client, db_session):
    role = db_session.query(models.Role).filter(models.Role.name == "Control Room Operator").first()
    user = models.User(
        username=f"audit_fix_op_{uuid.uuid4().hex[:6]}", password_hash=hash_password("testpass123"),
        full_name="Operator", role_id=role.id,
    )
    db_session.add(user)
    db_session.commit()
    resp = client.post("/api/alerts/does-not-exist/acknowledge", headers=_auth(create_access_token(user)))
    assert resp.status_code == 404  # past the role gate, on to the lookup


# --- 3. disabled rules stop firing ------------------------------------------

def _camera_zone_detection(db_session):
    camera = models.Camera(
        camera_code=f"C-RULEOFF-{uuid.uuid4().hex[:8]}", name="Rule Off", source_type="mock_vms", source_uri="",
    )
    db_session.add(camera)
    db_session.flush()
    zone = models.Zone(name="Gate", camera_id=camera.id, x1=0.0, y1=0.0, x2=1.0, y2=1.0, active=True)
    db_session.add(zone)
    db_session.flush()
    det = models.Detection(
        camera_id=camera.id, cls="person", confidence=0.9, bbox=[10, 10, 50, 50],
        source_timestamp=datetime.utcnow(), track_id=uuid.uuid4().hex[:6],
    )
    db_session.add(det)
    db_session.flush()
    return camera, zone, det


def test_zone_entry_fires_with_no_rule_row(db_session):
    rules_engine._alert_claims.clear()
    camera, _zone, det = _camera_zone_detection(db_session)
    alerts = asyncio.run(rules_engine.evaluate(db_session, camera, det, 640, 480))
    assert alerts and any("Restricted-zone entry" in r for r in alerts[0].reasons)


def test_disabled_zone_entry_rule_stops_alerts(db_session):
    rules_engine._alert_claims.clear()
    camera, zone, det = _camera_zone_detection(db_session)
    db_session.add(models.AlertRule(name="Gate entry", rule_type="zone_entry", zone_id=zone.id, active=False))
    db_session.commit()
    assert asyncio.run(rules_engine.evaluate(db_session, camera, det, 640, 480)) == []


def test_active_zone_entry_rule_still_fires(db_session):
    rules_engine._alert_claims.clear()
    camera, zone, det = _camera_zone_detection(db_session)
    db_session.add(models.AlertRule(name="Gate entry", rule_type="zone_entry", zone_id=zone.id, active=True))
    db_session.commit()
    alerts = asyncio.run(rules_engine.evaluate(db_session, camera, det, 640, 480))
    assert alerts and any("Restricted-zone entry" in r for r in alerts[0].reasons)


def test_rule_switched_off_semantics(db_session):
    rule_type = f"watchlist_plate_probe_{uuid.uuid4().hex[:6]}"
    assert rules_engine._rule_switched_off(db_session, rule_type) is False  # no rule: default-on
    db_session.add(models.AlertRule(name="a", rule_type=rule_type, active=False))
    db_session.flush()
    assert rules_engine._rule_switched_off(db_session, rule_type) is True
    db_session.add(models.AlertRule(name="b", rule_type=rule_type, active=True))
    db_session.flush()
    assert rules_engine._rule_switched_off(db_session, rule_type) is False
    db_session.rollback()


# --- 4. audit write survives a transient SQLite lock ------------------------

def test_log_action_retries_a_locked_database(db_session, admin_user, monkeypatch):
    from sqlalchemy.exc import OperationalError
    from app import audit

    real_commit = db_session.commit
    calls = {"n": 0}

    def flaky_commit():
        calls["n"] += 1
        if calls["n"] == 1:
            raise OperationalError("INSERT INTO audit_logs", {}, Exception("database is locked"))
        return real_commit()

    monkeypatch.setattr(db_session, "commit", flaky_commit)
    entry = audit.log_action(db_session, admin_user, "qa_lock_probe", resource="x")
    assert entry is not None and calls["n"] == 2


def test_log_action_still_raises_non_lock_errors(db_session, admin_user, monkeypatch):
    from sqlalchemy.exc import OperationalError
    from app import audit

    def broken_commit():
        raise OperationalError("INSERT INTO audit_logs", {}, Exception("no such table: audit_logs"))

    monkeypatch.setattr(db_session, "commit", broken_commit)
    with pytest.raises(OperationalError):
        audit.log_action(db_session, admin_user, "qa_broken_probe")
    db_session.rollback()


# --- 5. OCR runs outside the detection's write transaction -----------------

def test_ocr_runs_before_the_detection_flush(db_session, monkeypatch):
    import numpy as np
    from app.pipeline import worker

    order = []

    async def fake_ocr(*a, **k):
        order.append("ocr")
        return None

    async def fake_flush(*a, **k):
        order.append("flush")
        return False  # stop processing this detection right after the flush

    monkeypatch.setattr(worker, "_anpr_ocr", fake_ocr)
    monkeypatch.setattr(worker, "_safe_flush", fake_flush)
    monkeypatch.setattr(worker, "detect_and_track", lambda *a, **k: [
        {"cls": "car", "confidence": 0.9, "bbox": [1, 1, 20, 20], "track_id": 1},
    ])
    camera = models.Camera(
        camera_code=f"C-OCRFIRST-{uuid.uuid4().hex[:6]}", name="p", source_type="mock_vms", source_uri="",
        ai_person=False, ai_vehicle=True, ai_anpr=True,
    )
    frame = np.zeros((48, 64, 3), dtype=np.uint8)
    n = worker.settings.detect_every_n_frames
    asyncio.run(worker._process_frame(db_session, camera, frame, n, 64, 48, None, []))
    assert order == ["ocr", "flush"], order
    db_session.rollback()


# --- 6. deleting a camera clears its open Self-Heal problems -----------------

def test_deleted_camera_leaves_no_open_self_heal_problem(client, admin_token):
    from app.self_heal import engine as self_heal

    resp = client.post(
        "/api/cameras",
        json={"camera_code": f"C-SHDEL-{uuid.uuid4().hex[:6]}", "name": "x", "source_type": "mock_vms", "source_uri": ""},
        headers=_auth(admin_token),
    )
    assert resp.status_code == 200, resp.text
    camera_id = resp.json()["id"]
    client.post(f"/api/cameras/{camera_id}/stop", headers=_auth(admin_token))
    asyncio.run(self_heal.record_event(
        component="camera", camera_id=camera_id, error_type="CAMERA_CONNECT_FAILURE",
        severity="critical", message="probe", recovery_action="RECONNECT",
        attempt=5, max_attempts=5, status="FAILED",
    ))
    problems = client.get("/api/self-heal/problems", headers=_auth(admin_token)).json()
    assert any(p["camera_id"] == camera_id for p in problems)

    assert client.delete(f"/api/cameras/{camera_id}", headers=_auth(admin_token)).status_code == 200
    problems = client.get("/api/self-heal/problems", headers=_auth(admin_token)).json()
    assert not any(p["camera_id"] == camera_id for p in problems)


# --- 7. a WebSocket closes when its session token expires ---------------------

def test_websocket_closes_when_its_token_expires(client, admin_user):
    import time
    from datetime import datetime, timedelta
    from jose import jwt
    from starlette.websockets import WebSocketDisconnect
    from app.config import settings

    token = jwt.encode(
        {"sub": admin_user.id, "username": admin_user.username, "role": "Administrator",
         "exp": datetime.utcnow() + timedelta(seconds=2)},
        settings.jwt_secret, algorithm=settings.jwt_algorithm,
    )
    started = time.monotonic()
    with pytest.raises(WebSocketDisconnect) as exc:
        with client.websocket_connect(f"/ws?token={token}") as ws:
            ws.receive_text()
    assert exc.value.code == 4401
    assert time.monotonic() - started < 10


def test_websocket_with_long_lived_token_stays_open(client, admin_token):
    from app.ws import manager
    with client.websocket_connect(f"/ws?token={admin_token}"):
        assert len(manager.active) >= 1


# --- 8. alert exposes its detection; system actions are attributed ----------

def test_alert_api_exposes_the_detection_that_fired_it(client, admin_token, db_session):
    cam = models.Camera(camera_code=f"C-DET-{uuid.uuid4().hex[:6]}", name="d", source_type="mock_vms", source_uri="")
    db_session.add(cam)
    db_session.flush()
    det = models.Detection(camera_id=cam.id, cls="car", confidence=0.9, bbox=[1, 1, 2, 2])
    db_session.add(det)
    db_session.flush()
    alert = models.Alert(camera_id=cam.id, severity="HIGH", detection_id=det.id, reasons=["x"])
    db_session.add(alert)
    db_session.commit()
    body = client.get(f"/api/alerts/{alert.id}", headers=_auth(admin_token)).json()
    assert body["detection_id"] == det.id


def test_system_actions_are_audited_as_system(db_session):
    from app import audit
    entry = audit.log_action(db_session, None, "qa_system_probe", actor="system")
    assert entry.username == "system"
    assert audit.log_action(db_session, None, "qa_anon_probe").username == "anonymous"


# --- 9. evidence provenance follows the model actually loaded ----------------

def test_model_version_is_derived_from_the_model_in_use():
    from app.config import Settings
    assert Settings(model_name="yolov8n.pt").model_version == "yolov8n-coco-1.0"
    assert Settings(model_name="yolov8s.pt").model_version == "yolov8s-coco-1.0"
    assert Settings(model_name="models/custom.pt", model_version="plates-v3").model_version == "plates-v3"


# --- 10. correlation never merges unrelated camera events ------------------

def test_zone_alerts_on_different_cameras_open_separate_incidents(db_session):
    rules_engine._alert_claims.clear()
    incidents = []
    for _ in range(2):
        cam = models.Camera(camera_code=f"C-CORR-{uuid.uuid4().hex[:6]}", name="c", source_type="mock_vms", source_uri="")
        db_session.add(cam)
        db_session.flush()
        db_session.add(models.Zone(name="Z", camera_id=cam.id, x1=0, y1=0, x2=1, y2=1, active=True, severity="CRITICAL"))
        det = models.Detection(camera_id=cam.id, cls="person", confidence=0.9, bbox=[10, 10, 50, 50],
                               source_timestamp=datetime.utcnow(), track_id="1")
        db_session.add(det)
        db_session.commit()
        alerts = asyncio.run(rules_engine.evaluate(db_session, cam, det, 640, 480))
        assert alerts and alerts[0].severity == "CRITICAL"
        incidents.append(rules_engine.find_incident_for_alert(db_session, alerts[0].id).id)
    assert incidents[0] != incidents[1]
