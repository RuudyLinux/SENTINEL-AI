"""Adapter interface (Model 3 — VMS Federation/Middleware): the factory dispatches to
the right adapter class, the mock generic-VMS adapter produces real frames end-to-end,
and the ONVIF stub fails loudly instead of pretending to work."""
import pytest

from app.pipeline.adapters import (
    get_adapter, WebcamAdapter, VideoFileAdapter, RTSPAdapter, MockVMSAdapter, ONVIFAdapter,
)


@pytest.mark.parametrize("source_type,expected_cls", [
    ("webcam", WebcamAdapter),
    ("video_file", VideoFileAdapter),
    ("rtsp", RTSPAdapter),
    ("mock_vms", MockVMSAdapter),
    ("onvif", ONVIFAdapter),
])
def test_factory_dispatches_to_correct_adapter_class(source_type, expected_cls):
    adapter = get_adapter(source_type, "irrelevant-uri")
    assert isinstance(adapter, expected_cls)


def test_factory_rejects_unknown_source_type():
    with pytest.raises(ValueError):
        get_adapter("some_vendor_nobody_registered", "uri")


def test_mock_vms_adapter_opens_and_produces_real_frames():
    adapter = MockVMSAdapter("unused")
    assert adapter.open() is True
    ok, frame = adapter.read()
    assert ok is True
    assert frame is not None
    assert frame.shape == (480, 640, 3)
    assert adapter.fps() > 0
    assert adapter.resolution() == "640x480"
    adapter.release()
    ok2, frame2 = adapter.read()
    assert ok2 is False
    assert frame2 is None


def test_onvif_adapter_fails_loudly_instead_of_faking_success():
    adapter = ONVIFAdapter("rtsp://some-onvif-device/")
    with pytest.raises(NotImplementedError):
        adapter.open()
    # never silently reports success/frames for a source it never actually opened
    ok, frame = adapter.read()
    assert ok is False
    assert frame is None
