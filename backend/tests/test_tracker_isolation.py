"""10. Camera tracker isolation — P0-B: one YOLO/ByteTrack instance per
camera, never shared, so concurrent cameras cannot corrupt each other's
track IDs. YOLO itself is mocked out — this test is about the per-camera
instance bookkeeping, not the model weights."""
from app.pipeline import detector


class _FakeYOLO:
    _count = 0

    def __init__(self, *a, **kw):
        _FakeYOLO._count += 1
        self.instance_id = _FakeYOLO._count


def test_each_camera_gets_its_own_model_instance(monkeypatch):
    monkeypatch.setattr(detector, "YOLO", _FakeYOLO)
    detector._MODELS_BY_CAMERA.clear()

    model_a = detector.get_model("cam_A")
    model_b = detector.get_model("cam_B")
    assert model_a is not model_b
    assert model_a.instance_id != model_b.instance_id


def test_repeated_calls_for_the_same_camera_reuse_its_instance(monkeypatch):
    monkeypatch.setattr(detector, "YOLO", _FakeYOLO)
    detector._MODELS_BY_CAMERA.clear()

    first = detector.get_model("cam_A")
    second = detector.get_model("cam_A")
    assert first is second


def test_release_model_drops_the_instance_so_a_fresh_one_is_built_next(monkeypatch):
    monkeypatch.setattr(detector, "YOLO", _FakeYOLO)
    detector._MODELS_BY_CAMERA.clear()

    first = detector.get_model("cam_A")
    detector.release_model("cam_A")
    second = detector.get_model("cam_A")
    assert first is not second
