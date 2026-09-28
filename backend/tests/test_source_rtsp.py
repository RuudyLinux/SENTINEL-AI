"""RTSP: TCP transport is set before open()."""
import os

from app.pipeline import source as source_mod
from app.pipeline.source import CameraSource
from app.pipeline import adapters
from app.config import settings


class _FakeCapture:
    def __init__(self, *a, **kw):
        pass

    def isOpened(self):
        return True

    def set(self, *a, **kw):
        return True


def test_rtsp_open_sets_tcp_transport_env_before_videocapture(monkeypatch):
    monkeypatch.delenv("OPENCV_FFMPEG_CAPTURE_OPTIONS", raising=False)
    seen = {}

    def fake_video_capture(uri, backend=None):
        # captured here, so the env var was set before this call
        seen["env_at_open"] = os.environ.get("OPENCV_FFMPEG_CAPTURE_OPTIONS")
        return _FakeCapture()

    monkeypatch.setattr(source_mod.cv2, "VideoCapture", fake_video_capture)

    src = CameraSource("rtsp", "rtsp://example.invalid:8554/stream/1")
    assert src.open() is True
    assert seen["env_at_open"] == "rtsp_transport;tcp"
    assert src.transport_forced_tcp is True


def test_rtsp_transport_not_forced_when_disabled(monkeypatch):
    monkeypatch.delenv("OPENCV_FFMPEG_CAPTURE_OPTIONS", raising=False)
    monkeypatch.setattr(source_mod.settings, "rtsp_force_tcp", False)
    monkeypatch.setattr(source_mod.cv2, "VideoCapture", lambda uri, backend=None: _FakeCapture())

    src = CameraSource("rtsp", "rtsp://example.invalid:8554/stream/1")
    src.open()
    assert src.transport_forced_tcp is False
    assert os.environ.get("OPENCV_FFMPEG_CAPTURE_OPTIONS") is None


def test_udp_transport_sets_udp_and_a_bigger_buffer(monkeypatch):
    seen = {}

    class FakeCap:
        def __init__(self, *a, **k):
            seen["opts"] = os.environ.get("OPENCV_FFMPEG_CAPTURE_OPTIONS")

        def set(self, *a):
            return True

        def isOpened(self):
            return True

    monkeypatch.setattr(adapters.cv2, "VideoCapture", FakeCap)
    monkeypatch.delenv("OPENCV_FFMPEG_CAPTURE_OPTIONS", raising=False)
    a = adapters.RTSPAdapter("rtsp://example.invalid/x", transport="udp")
    assert a.open() is True
    assert seen["opts"] == f"rtsp_transport;udp|buffer_size;{settings.rtsp_udp_buffer_bytes}"
    assert a.transport_forced_tcp is False
