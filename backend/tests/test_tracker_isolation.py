"""One YOLO model shared by every camera, one ByteTrack tracker per camera,
so cameras can't corrupt each other's track ids and GPU memory doesn't grow
with the number of cameras. YOLO is mocked."""
import numpy as np

from app.pipeline import detector


class _FakeYOLO:
    count = 0

    def __init__(self, *a, **kw):
        _FakeYOLO.count += 1

    def predict(self, *a, **kw):
        return []


def _fresh(monkeypatch):
    monkeypatch.setattr(detector, "YOLO", _FakeYOLO)
    monkeypatch.setattr(detector, "_MODEL", None)
    _FakeYOLO.count = 0
    detector._TRACKERS.clear()


def test_every_camera_shares_one_model(monkeypatch):
    _fresh(monkeypatch)
    frame = np.zeros((32, 32, 3), dtype=np.uint8)
    detector.detect_and_track(frame, "cam_A")
    detector.detect_and_track(frame, "cam_B")
    assert _FakeYOLO.count == 1
    assert detector.get_model() is detector.get_model()


def test_each_camera_gets_its_own_tracker(monkeypatch):
    _fresh(monkeypatch)
    assert detector._tracker("cam_A") is not detector._tracker("cam_B")
    assert detector._tracker("cam_A") is detector._tracker("cam_A")


def test_release_drops_only_that_cameras_tracker(monkeypatch):
    _fresh(monkeypatch)
    a, b = detector._tracker("cam_A"), detector._tracker("cam_B")
    detector.release_model("cam_A")
    assert detector._tracker("cam_A") is not a
    assert detector._tracker("cam_B") is b
    assert _FakeYOLO.count == 0  # releasing never touches the shared model
