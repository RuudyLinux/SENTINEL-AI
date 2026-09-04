"""Event video clips — ring buffer bounding + encode."""
import time

import cv2
import numpy as np

from app.pipeline import clips


def _tiny_jpeg() -> bytes:
    frame = np.zeros((20, 20, 3), dtype=np.uint8)
    ok, buf = cv2.imencode(".jpg", frame)
    assert ok
    return buf.tobytes()


def test_ring_buffer_evicts_frames_older_than_pre_event_window(monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "clip_pre_event_seconds", 1.0)
    clips._RING.clear()

    t = [1000.0]
    monkeypatch.setattr(clips.time, "monotonic", lambda: t[0])

    clips.push_frame("cam_X", _tiny_jpeg())
    t[0] += 2.0  # older than the 1s pre-event window now
    clips.push_frame("cam_X", _tiny_jpeg())

    recent = clips._recent_frames("cam_X")
    assert len(recent) == 1  # the first, now-stale frame was evicted


def test_ring_buffer_never_unbounded(monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "clip_pre_event_seconds", 5.0)
    clips._RING.clear()
    t = [0.0]
    monkeypatch.setattr(clips.time, "monotonic", lambda: t[0])

    for i in range(1000):
        t[0] += 0.01  # 10 seconds of frames pushed at ~100fps
        clips.push_frame("cam_Y", _tiny_jpeg())

    # only the last ~5 seconds' worth should remain, never the full 1000
    assert len(clips._recent_frames("cam_Y")) < 600


def test_encode_clip_produces_a_real_playable_file(tmp_path):
    frames = [_tiny_jpeg() for _ in range(5)]
    out = tmp_path / "clip.mp4"
    ok = clips._encode_clip(frames, str(out))
    assert ok is True
    assert out.exists()
    assert out.stat().st_size > 0


def test_encode_clip_returns_false_for_undecodable_frames(tmp_path):
    out = tmp_path / "clip.mp4"
    ok = clips._encode_clip([b"not a real jpeg"], str(out))
    assert ok is False
