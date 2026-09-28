"""REC button: the annotated live view recorded to MP4 and saved as hashed
evidence. Real ffmpeg, frames faked as real JPEGs."""
import time
import uuid
from pathlib import Path

import cv2
import numpy as np
import pytest

from app import models
from app.config import settings
from app.evidence_hash import sha256_file
from app.pipeline import recorder, worker


def _jpeg(shade: int, size=(64, 48)) -> bytes:
    frame = np.full((size[1], size[0], 3), shade, dtype=np.uint8)
    ok, buf = cv2.imencode(".jpg", frame)
    assert ok
    return buf.tobytes()


@pytest.fixture(autouse=True)
def _fast(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "recording_fps", 20.0)
    monkeypatch.setattr(settings, "evidence_dir", tmp_path)
    yield
    recorder.stop_all(timeout=30)
    recorder._ACTIVE.clear()


def _camera(db_session):
    cam = models.Camera(camera_code=f"C-REC-{uuid.uuid4().hex[:6]}", name="rec", source_type="mock_vms", source_uri="")
    db_session.add(cam)
    db_session.commit()
    db_session.refresh(cam)
    return cam


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


def test_a_recording_becomes_hashed_evidence(db_session):
    cam = _camera(db_session)
    frames = [_jpeg(40), _jpeg(200)]
    rec = recorder.start(cam.id, cam.camera_code, lambda: frames[int(time.monotonic() * 10) % 2])
    time.sleep(1.0)
    rec = recorder.stop(cam.id)
    assert rec.error == ""
    assert rec.evidence_id
    assert not recorder.is_recording(cam.id)
    ev = db_session.get(models.Evidence, rec.evidence_id)
    assert ev.evidence_type == "recording"
    assert ev.camera_id == cam.id
    assert Path(ev.file_path).stat().st_size > 0
    assert ev.sha256 == sha256_file(ev.file_path)
    # plays in real time: ~1s at 20fps, give or take scheduling
    cap = cv2.VideoCapture(ev.file_path)
    n = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    cap.release()
    assert 10 <= n <= 30


def test_no_picture_means_no_recording(db_session):
    cam = _camera(db_session)
    with pytest.raises(recorder.RecordingError, match="no live picture"):
        recorder.start(cam.id, cam.camera_code, lambda: None)


def test_one_recording_per_camera_and_a_global_cap(db_session, monkeypatch):
    monkeypatch.setattr(settings, "recording_max_concurrent", 2)
    frame = _jpeg(90)
    a, b, c = _camera(db_session), _camera(db_session), _camera(db_session)
    recorder.start(a.id, a.camera_code, lambda: frame)
    with pytest.raises(recorder.RecordingError, match="already recording"):
        recorder.start(a.id, a.camera_code, lambda: frame)
    recorder.start(b.id, b.camera_code, lambda: frame)
    with pytest.raises(recorder.RecordingError, match="limit 2"):
        recorder.start(c.id, c.camera_code, lambda: frame)


def test_it_stops_itself_at_the_maximum_length(db_session, monkeypatch):
    monkeypatch.setattr(settings, "recording_max_seconds", 0.5)
    cam = _camera(db_session)
    rec = recorder.start(cam.id, cam.camera_code, lambda: _jpeg(120))
    rec.thread.join(30)
    assert rec.stop_reason == "maximum length reached"
    assert rec.evidence_id


def test_stopping_the_camera_finishes_the_file(db_session):
    cam = _camera(db_session)
    rec = recorder.start(cam.id, cam.camera_code, lambda: _jpeg(60))
    time.sleep(0.3)
    worker.stop_worker(cam.id)
    rec.thread.join(30)
    assert rec.stop_reason == "camera stopped"
    assert rec.evidence_id


def test_rec_over_the_api(client, admin_token, db_session):
    cam = _camera(db_session)
    worker.LATEST_FRAMES[cam.id] = _jpeg(150)
    try:
        started = client.post(f"/api/cameras/{cam.id}/recording/start", headers=_auth(admin_token))
        assert started.status_code == 200, started.text
        assert started.json()["recording"] is True
        assert client.get(f"/api/cameras/{cam.id}", headers=_auth(admin_token)).json()["recording"] is True
        assert client.post(f"/api/cameras/{cam.id}/recording/start", headers=_auth(admin_token)).status_code == 409
        time.sleep(0.5)
        stopped = client.post(f"/api/cameras/{cam.id}/recording/stop", headers=_auth(admin_token)).json()
        assert stopped["recording"] is False
        assert stopped["evidence_id"]
        ev = client.get("/api/evidence", headers=_auth(admin_token)).json()
        assert any(e["id"] == stopped["evidence_id"] and e["evidence_type"] == "recording" for e in ev)
    finally:
        worker.LATEST_FRAMES.pop(cam.id, None)


def test_rec_needs_an_operational_role(client, db_session):
    from app.security import create_access_token, hash_password
    role = db_session.query(models.Role).filter(models.Role.name == "Auditor").first()
    user = models.User(username=f"aud-{uuid.uuid4().hex[:6]}", password_hash=hash_password("x" * 12), role_id=role.id)
    db_session.add(user)
    db_session.commit()
    cam = _camera(db_session)
    db_session.refresh(user)
    token = create_access_token(user)
    assert client.post(f"/api/cameras/{cam.id}/recording/start", headers=_auth(token)).status_code == 403
