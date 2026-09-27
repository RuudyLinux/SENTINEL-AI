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


def test_clip_plays_at_the_rate_frames_were_captured(tmp_path):
    """Frames captured at 2.5fps must make a clip of real duration. Encoding at
    the fixed nominal clip_fps (10) played evidence 4x too fast once the camera
    loop started taking only the newest frame."""
    import imageio_ffmpeg

    timed = [(100.0 + i * 0.4, _tiny_jpeg()) for i in range(11)]  # 4s at 2.5fps
    fps = clips._playback_fps(timed)
    assert abs(fps - 2.5) < 1e-6
    out = tmp_path / "timed.mp4"
    assert clips._encode_clip([f for _, f in timed], str(out), fps) is True
    _frames, seconds = imageio_ffmpeg.count_frames_and_secs(str(out))
    assert 3.5 <= seconds <= 5.0, seconds


def test_playback_fps_falls_back_for_degenerate_input():
    assert clips._playback_fps([]) == clips.settings.clip_fps
    assert clips._playback_fps([(1.0, b"x"), (1.0, b"y")]) == clips.settings.clip_fps


def test_encode_holds_one_decoded_frame_at_a_time(tmp_path, monkeypatch):
    """Decoding the whole batch before encoding held every raw frame at once
    (6.2 MB each at 1080p) and ran the demo machine out of memory when a busy
    zone raised several clips together. Each frame must be decoded only when
    it is about to be written."""
    real_imdecode = cv2.imdecode
    live = []

    def counting_imdecode(buf, flags):
        live.append(1)
        return real_imdecode(buf, flags)

    monkeypatch.setattr(clips.cv2, "imdecode", counting_imdecode)
    decoded_when_written = []
    real_chain = clips.itertools.chain

    def watching_chain(*its):
        for frame in real_chain(*its):
            decoded_when_written.append(len(live))
            yield frame

    monkeypatch.setattr(clips.itertools, "chain", watching_chain)

    frames = [_tiny_jpeg() for _ in range(6)]
    frames.insert(3, b"not a real jpeg")  # an undecodable frame mid-clip is skipped
    assert clips._encode_clip(frames, str(tmp_path / "lazy.mp4")) is True
    # When frame k (0-based) is written, at most k+2 frames have been decoded
    # (the undecodable one included) -- never the whole batch up front.
    assert decoded_when_written[0] == 1
    assert all(seen <= k + 2 for k, seen in enumerate(decoded_when_written))
    assert len(decoded_when_written) == 6
