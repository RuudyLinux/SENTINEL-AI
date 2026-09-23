"""Two defects that only ever announced themselves as warnings.

A warning is not noise when it names your own module. Both of these did, and
both were real:

1. `ws.py` built a coroutine and then threw it away whenever there was no
   running event loop. Python said so on every synchronous publish
   ("coroutine '_flush_loop' was never awaited").
2. `clips.py` opened an ffmpeg stderr pipe it never read or closed. The
   unclosed handle was the visible half (a ResourceWarning); the unread half
   is worse, because a pipe nobody drains fills up and blocks the process
   writing to it.

Warnings are escalated to errors here rather than asserted on text, so these
stay failures rather than drifting back into the suite's warning summary.
"""
import asyncio
import subprocess
import warnings

import numpy as np

from app import ws as ws_module
from app.pipeline import clips


class TestFlushTaskWithoutALoop:
    """`_ensure_flush_task` is reached from synchronous producer code."""

    def test_buffering_off_the_loop_creates_no_orphan_coroutine(self):
        manager = ws_module.ConnectionManager()
        with warnings.catch_warnings():
            warnings.simplefilter("error", RuntimeWarning)
            manager._buffer("detection", "detection_batch", {"id": "d1"})
        assert manager._flush_task is None, "no loop means no task, and no coroutine either"

    def test_the_event_is_still_buffered(self):
        """The guard must not cost the buffering it protects — the event has
        to survive to the next publish that does have a loop."""
        manager = ws_module.ConnectionManager()
        manager._buffer("detection", "detection_batch", {"id": "d1"})
        assert manager._buffers["detection_batch"] == [{"type": "detection", "id": "d1"}]

    def test_repeated_buffering_stays_clean(self):
        """The orphan was created once per call, so one call proves little."""
        manager = ws_module.ConnectionManager()
        with warnings.catch_warnings():
            warnings.simplefilter("error", RuntimeWarning)
            for i in range(25):
                manager._buffer("detection", "detection_batch", {"id": f"d{i}"})

    def test_a_task_is_still_created_when_a_loop_exists(self):
        """The fix is a guard, not a removal."""

        async def scenario():
            manager = ws_module.ConnectionManager()
            manager._buffer("detection", "detection_batch", {"id": "d1"})
            created = manager._flush_task is not None
            if manager._flush_task is not None:
                manager._flush_task.cancel()
            return created

        assert asyncio.run(scenario()) is True


def _tiny_frames(n: int = 4) -> list[bytes]:
    import cv2

    frames = []
    for _ in range(n):
        ok, buf = cv2.imencode(".jpg", np.zeros((32, 32, 3), dtype=np.uint8))
        assert ok
        frames.append(buf.tobytes())
    return frames


class TestClipEncoderPipes:
    def test_a_successful_encode_leaves_no_open_pipe(self, tmp_path):
        out = tmp_path / "clip.mp4"
        with warnings.catch_warnings():
            warnings.simplefilter("error", ResourceWarning)
            assert clips._encode_clip(_tiny_frames(), str(out)) is True
        assert out.exists() and out.stat().st_size > 0

    def test_stderr_is_drained_not_merely_closed(self, tmp_path, monkeypatch):
        """The deadlock case: ffmpeg writing more to stderr than the pipe
        buffer holds. `-loglevel error` keeps the real command quiet, so the
        protection has to be verified against a command that is not."""
        captured = {}
        # Bound BEFORE patching: `clips.subprocess` is the stdlib module
        # itself, so a replacement that calls `subprocess.Popen` by name would
        # call itself.
        real_popen = subprocess.Popen

        def noisy_popen(cmd, **kwargs):
            # Same ffmpeg, same input, but verbose enough to fill a pipe.
            loud = list(cmd)
            loud[loud.index("-loglevel") + 1] = "debug"
            captured["cmd"] = loud
            return real_popen(loud, **kwargs)

        monkeypatch.setattr(subprocess, "Popen", noisy_popen)
        out = tmp_path / "loud.mp4"
        # Without draining this blocks until the 60s timeout rather than
        # returning; the test would hang instead of failing, which is exactly
        # how the bug would have shown up in production.
        assert clips._encode_clip(_tiny_frames(40), str(out)) is True
        assert captured["cmd"][captured["cmd"].index("-loglevel") + 1] == "debug"

    def test_a_failing_encode_reports_the_reason(self, tmp_path, caplog, monkeypatch):
        """A bare False was indistinguishable from 'nothing decoded'.

        A rejected argument kills ffmpeg while frames are still being written,
        so this exercises the exception path — the one that was silent.
        """
        real_popen = subprocess.Popen

        def broken_popen(cmd, **kwargs):
            bad = list(cmd)
            bad.insert(1, "-not-a-real-flag")
            return real_popen(bad, **kwargs)

        monkeypatch.setattr(subprocess, "Popen", broken_popen)
        with caplog.at_level("WARNING"):
            assert clips._encode_clip(_tiny_frames(), str(tmp_path / "bad.mp4")) is False
        assert any("clip encode failed" in r.getMessage() for r in caplog.records)

    def test_a_nonzero_exit_reports_the_reason(self, tmp_path, caplog, monkeypatch):
        """The other failure shape: ffmpeg accepts the frames, then exits
        non-zero (here, an output container it cannot write)."""
        real_popen = subprocess.Popen

        def bad_output_popen(cmd, **kwargs):
            bad = list(cmd)
            bad[-1] = str(tmp_path / "clip.not-a-container")
            return real_popen(bad, **kwargs)

        monkeypatch.setattr(subprocess, "Popen", bad_output_popen)
        with caplog.at_level("WARNING"):
            assert clips._encode_clip(_tiny_frames(), str(tmp_path / "x.mp4")) is False
        assert any("clip encode failed" in r.getMessage() for r in caplog.records)

    def test_no_frames_still_returns_false_without_spawning_ffmpeg(self, tmp_path):
        """Unchanged behaviour: the early return happens before any process."""
        assert clips._encode_clip([b"not a jpeg"], str(tmp_path / "none.mp4")) is False
