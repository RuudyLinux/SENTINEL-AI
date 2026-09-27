"""Latest-frame reader: the live picture must stay current when AI is slower
than the camera.

Before this existed the camera loop read one frame, processed it, then read the
next, so a 30fps stream consumed at ~5fps queued up: measured on the real grid,
the picture ran at ~0.2x real time. These tests pin the replacement contract —
newest frame wins, old frames are dropped rather than queued, memory is one
frame, and the capture is released by the thread that owns it.
"""
import asyncio
import threading
import time
import uuid

import numpy as np

from app import models
from app.pipeline import worker
from app.pipeline.frame_reader import LatestFrameReader


class FastCamera:
    """A live source producing a new frame every `interval` seconds, each
    stamped with its own sequence number in pixel [0, 0, 0]."""

    def __init__(self, interval=0.002, fail_after=None):
        self.interval = interval
        self.n = 0
        self.fail_after = fail_after
        self.released = threading.Event()
        self.release_thread = None
        self.reader_threads = set()

    def read(self):
        self.reader_threads.add(threading.current_thread().name)
        time.sleep(self.interval)
        if self.fail_after is not None and self.n >= self.fail_after:
            return False, None
        self.n += 1
        frame = np.zeros((4, 4, 3), dtype=np.uint8)
        frame[0, 0, 0] = self.n % 256
        return True, frame

    def pos_msec(self):
        return self.n * self.interval * 1000

    def release(self):
        self.release_thread = threading.current_thread().name
        self.released.set()


def test_slow_consumer_gets_the_newest_frame_and_old_ones_are_dropped():
    cam = FastCamera(interval=0.002)
    reader = LatestFrameReader(cam, "t1")
    reader.start()
    try:
        seq, taken = 0, []
        for _ in range(5):
            time.sleep(0.1)  # "inference": ~50 camera frames arrive meanwhile
            got = reader.next_frame(seq, 0, timeout=2)
            assert got.frame is not None
            assert got.seq > seq  # strictly forward: the tracker sees capture order
            taken.append(got.seq)
            seq = got.seq
            # what we got is the newest decoded frame, not a queued old one
            assert got.seq >= reader.frames_read - 1
            assert got.age_ms < 50
        assert taken == sorted(taken)
        # far more frames decoded than consumed: the rest were dropped, not queued
        assert reader.frames_read > 5 * len(taken)
    finally:
        reader.stop()
        assert reader.join(2)


def test_reader_holds_one_frame_not_a_backlog():
    cam = FastCamera(interval=0.001)
    reader = LatestFrameReader(cam, "t2")
    reader.start()
    time.sleep(0.3)
    reader.stop()
    reader.join(2)
    # the only frame reference kept is the latest one
    frames = [v for v in vars(reader).values() if isinstance(v, np.ndarray)]
    assert len(frames) == 1


def test_stop_releases_the_source_on_the_reader_thread():
    cam = FastCamera()
    reader = LatestFrameReader(cam, "t3")
    reader.start()
    time.sleep(0.05)
    reader.stop()
    assert reader.join(2)
    assert cam.released.is_set()
    # every read and the release happened on the single owning thread
    assert cam.reader_threads == {"reader-t3"}
    assert cam.release_thread == "reader-t3"


def test_failed_reads_are_reported_not_hidden():
    cam = FastCamera(fail_after=3)
    reader = LatestFrameReader(cam, "t4")
    reader.start()
    try:
        seq = fail = 0
        seen_failure = False
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and not seen_failure:
            got = reader.next_frame(seq, fail, timeout=1)
            if got.frame is None:
                seen_failure = not got.timed_out
            seq, fail = max(seq, got.seq), got.fail_seq
        assert seen_failure
    finally:
        reader.stop()
        reader.join(2)


def test_a_stalled_stream_times_out():
    class Stalled(FastCamera):
        def read(self):
            time.sleep(5)
            return False, None

    reader = LatestFrameReader(Stalled(), "t5")
    reader.start()
    started = time.monotonic()
    got = reader.next_frame(0, 0, timeout=0.3)
    assert got.frame is None and got.timed_out
    assert time.monotonic() - started < 1
    reader.stop()


def test_self_paced_source_is_not_read_faster_than_its_fps():
    cam = FastCamera(interval=0)
    reader = LatestFrameReader(cam, "t6", pace_fps=20)
    reader.start()
    time.sleep(0.5)
    reader.stop()
    reader.join(2)
    assert reader.frames_read <= 13  # ~10 expected at 20fps over 0.5s


def test_two_cameras_have_independent_readers():
    a, b = FastCamera(interval=0.002), FastCamera(interval=0.004)
    ra, rb = LatestFrameReader(a, "cam-a"), LatestFrameReader(b, "cam-b")
    ra.start(), rb.start()
    time.sleep(0.2)
    rb.stop()
    rb.join(2)
    frames_b = rb.frames_read
    time.sleep(0.1)
    got = ra.next_frame(0, 0, timeout=1)  # a keeps going after b stopped
    assert got.frame is not None and ra.frames_read > frames_b
    assert a.reader_threads == {"reader-cam-a"} and b.reader_threads == {"reader-cam-b"}
    assert b.released.is_set() and not a.released.is_set()
    ra.stop()
    ra.join(2)


def test_camera_loop_drops_frames_and_stays_current_under_slow_ai(db_session, monkeypatch):
    """End to end through the real camera loop: a fast live source and
    deliberately slow processing. The loop must keep up with the newest frame
    (frames dropped, frame age small) instead of falling behind."""
    cam_row = models.Camera(
        camera_code=f"C-LAG-{uuid.uuid4().hex[:6]}", name="lag", source_type="rtsp",
        source_uri="rtsp://example.invalid/x", ai_person=False, ai_vehicle=False, ai_anpr=False,
    )
    db_session.add(cam_row)
    db_session.commit()
    source = FastCamera(interval=0.005)

    class FakeCameraSource:
        def __init__(self, *_a):
            pass

        def open(self):
            return True

        read, pos_msec, release = source.read, source.pos_msec, source.release

        def fps(self):
            return 200.0

        def resolution(self):
            return "4x4"

    async def slow_process(db, camera, frame, *a, **k):
        await asyncio.sleep(0.1)  # 10fps processing vs a 200fps camera
        return []

    monkeypatch.setattr(worker, "CameraSource", FakeCameraSource)
    monkeypatch.setattr(worker, "_process_frame", slow_process)

    async def run():
        task = asyncio.create_task(worker._camera_loop(cam_row.id))
        await asyncio.sleep(1.5)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    asyncio.run(run())
    st = worker._stats(cam_row.id)
    assert st["frames_read"] >= 5
    assert st["frames_dropped"] > st["frames_read"]  # most camera frames skipped
    assert st["frame_age_ms"] is not None and st["frame_age_ms"] < 100
    assert source.released.wait(3)  # shutdown freed the capture
