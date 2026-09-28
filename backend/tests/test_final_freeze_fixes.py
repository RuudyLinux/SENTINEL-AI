"""Regressions for the 2026-09-28 performance and runtime fixes.

- The per-frame JPEG work moved off the event loop and down to the rate its
  consumers read it; measured before: one 1080p camera at 23 fps spent ~68% of
  the event loop on it and every API call waited ~600 ms.
- The AI rate and AI-camera limit now follow the hardware unless set.
- ensure_indexes can create the composite index the live camera page needs.
"""
import cv2
import numpy as np
import pytest

from app.config import Settings
from app.db import IS_SQLITE, engine, ensure_indexes
from app.pipeline import worker


def _cuda() -> bool:
    import torch
    return bool(torch.cuda.is_available())


def test_ai_rate_and_limit_follow_the_hardware_when_unset():
    s = Settings(detect_every_n_frames=None, max_ai_cameras=None)
    if _cuda():
        assert (s.detect_every_n_frames, s.max_ai_cameras) == (1, 2)
    else:
        assert (s.detect_every_n_frames, s.max_ai_cameras) == (3, 1)


def test_explicit_ai_rate_and_limit_are_kept():
    s = Settings(detect_every_n_frames=5, max_ai_cameras=0)
    assert (s.detect_every_n_frames, s.max_ai_cameras) == (5, 0)


def test_preview_encoders_return_jpegs_and_leave_the_frame_untouched():
    frame = np.zeros((48, 64, 3), dtype=np.uint8)
    raw = worker._encode_jpeg(frame)
    annotated = worker._encode_annotated(
        frame, [{"cls": "car", "confidence": 0.9, "bbox": [4, 4, 40, 30], "track_id": 1}]
    )
    for jpeg in (raw, annotated):
        assert jpeg is not None and jpeg[:2] == b"\xff\xd8"
    assert cv2.imdecode(np.frombuffer(annotated, np.uint8), cv2.IMREAD_COLOR).any()
    assert not frame.any(), "drawing boxes must not write into the frame the AI reads"


def test_preview_rate_matches_what_its_consumers_read():
    # The MJPEG stream sends at most 10 frames/s; encoding faster is waste.
    assert worker._PREVIEW_INTERVAL_S == pytest.approx(0.1)


@pytest.mark.skipif(not IS_SQLITE, reason="ensure_indexes is SQLite-only")
def test_ensure_indexes_creates_a_composite_index():
    assert ensure_indexes("detections", [("camera_id", "timestamp")]) == ["ix_detections_camera_id_timestamp"]
    with engine.connect() as conn:
        cols = [r[2] for r in conn.exec_driver_sql("PRAGMA index_info('ix_detections_camera_id_timestamp')")]
    assert cols == ["camera_id", "timestamp"]


@pytest.mark.skipif(not IS_SQLITE, reason="query plan check is SQLite-specific")
def test_newest_detections_for_a_camera_use_the_composite_index():
    ensure_indexes("detections", [("camera_id", "timestamp")])
    with engine.connect() as conn:
        plan = " ".join(r[3] for r in conn.exec_driver_sql(
            "EXPLAIN QUERY PLAN SELECT * FROM detections WHERE camera_id = 'c' ORDER BY timestamp DESC LIMIT 50"
        ))
    assert "ix_detections_camera_id_timestamp" in plan
    assert "TEMP B-TREE" not in plan, plan
