"""AI capacity guard: the machine runs at most MAX_AI_CAMERAS AI cameras.

Measured: one AI camera holds the demo CPU at ~92%, a second saturates it and
the whole system degrades. Connecting is cheap and stays allowed; AI is what is
limited — at the API with a clear refusal, and inside the worker so no path
(bulk, PATCH, startup, supervisor) can exceed it.
"""
import asyncio
import uuid

import numpy as np
import pytest

from app import models
from app.config import settings
from app.pipeline import ai_capacity, worker


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture(autouse=True)
def _clean_slots(monkeypatch):
    ai_capacity._HOLDERS.clear()
    monkeypatch.setattr(settings, "max_ai_cameras", 1)
    yield
    ai_capacity._HOLDERS.clear()


def _camera(db_session, ai=True):
    cam = models.Camera(camera_code=f"C-CAP-{uuid.uuid4().hex[:6]}", name="c", source_type="mock_vms",
                        source_uri="", ai_person=ai, ai_vehicle=ai, ai_anpr=False)
    db_session.add(cam)
    db_session.commit()
    db_session.refresh(cam)
    return cam


def test_slots_are_limited_and_reusable():
    assert ai_capacity.try_acquire("a") is True
    assert ai_capacity.try_acquire("a") is True          # idempotent for the holder
    assert ai_capacity.try_acquire("b") is False
    assert "AI capacity limit reached" in ai_capacity.blocked("b")
    assert ai_capacity.blocked("a") is None
    ai_capacity.release("a")
    assert ai_capacity.try_acquire("b") is True


def test_worker_streams_without_inference_when_no_slot_is_free(db_session, monkeypatch):
    calls = []
    monkeypatch.setattr(worker, "detect_and_track", lambda *a, **k: calls.append(1) or [])
    ai_capacity.try_acquire("someone-else")
    cam = _camera(db_session)
    frame = np.zeros((32, 32, 3), dtype=np.uint8)
    asyncio.run(worker._process_frame(db_session, cam, frame, settings.detect_every_n_frames, 32, 32, None, []))
    assert calls == []
    assert worker._stats(cam.id)["ai_blocked"] is True
    ai_capacity.release("someone-else")
    asyncio.run(worker._process_frame(db_session, cam, frame, settings.detect_every_n_frames * 2, 32, 32, None, []))
    assert calls == [1]
    assert cam.id in ai_capacity.holders()


def test_stopping_a_worker_frees_its_slot(db_session):
    ai_capacity.try_acquire("cam-x")
    worker.stop_worker("cam-x")
    assert "cam-x" not in ai_capacity.holders()


def test_bulk_start_ai_is_refused_at_capacity(client, admin_token, db_session):
    ai_capacity.try_acquire("already-running")
    cam = _camera(db_session, ai=False)
    body = client.post("/api/cameras/bulk", json={"action": "start_ai", "camera_ids": [cam.id]},
                       headers=_auth(admin_token)).json()
    assert body["successful"] == 0
    assert "AI capacity limit reached" in body["results"][0]["detail"]
    db_session.expire_all()
    assert db_session.get(models.Camera, cam.id).ai_person is False  # AI was not switched on


def test_connect_at_capacity_streams_without_ai_and_says_so(client, admin_token, db_session):
    ai_capacity.try_acquire("already-running")
    cam = _camera(db_session, ai=True)
    body = client.post("/api/cameras/bulk", json={"action": "connect", "camera_ids": [cam.id]},
                       headers=_auth(admin_token)).json()
    assert body["results"][0]["ok"] is True
    assert "Connected without AI" in body["results"][0]["detail"]
    client.post("/api/cameras/bulk", json={"action": "disconnect", "camera_ids": [cam.id]}, headers=_auth(admin_token))


def test_patch_enabling_ai_on_a_running_camera_is_refused_at_capacity(client, admin_token, db_session, monkeypatch):
    cam = _camera(db_session, ai=False)
    ai_capacity.try_acquire("already-running")

    class _Alive:
        def done(self):
            return False
    monkeypatch.setitem(worker.RUNNING, cam.id, _Alive())
    resp = client.patch(f"/api/cameras/{cam.id}", json={"ai_person": True}, headers=_auth(admin_token))
    worker.RUNNING.pop(cam.id, None)
    assert resp.status_code == 409
    assert "AI capacity limit reached" in resp.json()["detail"]


def test_a_worker_that_crashes_frees_its_slot(monkeypatch):
    async def crash(camera_id):
        ai_capacity.try_acquire(camera_id)
        raise RuntimeError("source could not be opened")

    async def noop(**kwargs):
        return None

    monkeypatch.setattr(worker, "_camera_loop", crash)
    monkeypatch.setattr(worker.self_heal, "record_event", noop)
    asyncio.run(worker._camera_loop_supervised("cam-crash"))
    assert "cam-crash" not in ai_capacity.holders()


def test_a_cancelled_old_worker_does_not_free_the_new_workers_slot(monkeypatch):
    async def scenario():
        started = asyncio.Event()

        async def loop(camera_id):
            ai_capacity.try_acquire(camera_id)
            started.set()
            await asyncio.sleep(3600)

        monkeypatch.setattr(worker, "_camera_loop", loop)
        old = asyncio.create_task(worker._camera_loop_supervised("cam-r"))
        await started.wait()
        worker.RUNNING["cam-r"] = asyncio.create_task(asyncio.sleep(3600))  # the restarted worker
        old.cancel()
        await asyncio.gather(old, return_exceptions=True)
        held = "cam-r" in ai_capacity.holders()
        worker.RUNNING.pop("cam-r").cancel()
        return held

    assert asyncio.run(scenario()) is True
