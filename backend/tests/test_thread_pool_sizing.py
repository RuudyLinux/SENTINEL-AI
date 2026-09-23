"""The shared thread pool has to know how many cameras exist.

`asyncio.to_thread` uses the event loop's default executor, and in this
process that one pool serves every blocking call there is: each camera
worker's `source.read()`, every inference offload, every DB commit through
db_retry, evidence hashing, and the connection probes.

Python sizes that pool `min(32, cpu_count + 4)` and never reconsiders. On a
16-core machine that is 20 threads — a fixed ceiling, with no relationship to
the number of registered cameras. Measured here with all 34 cameras
connected: the count reporting `online` oscillated (12, then 4) while every
one of them recorded `last_error: none`. Nothing was failing. The reads were
queued behind each other, and a read waiting for a thread is indistinguishable
from a read waiting for a camera — which is why this looked like a camera
problem for as long as it did.
"""
from app.config import settings
from app.main import _size_thread_pool


class TestAutomaticSizing:
    def test_every_camera_can_hold_a_thread_at_once(self):
        """The property that matters: cameras + headroom, never fewer."""
        for cameras in (34, 60, 100):
            assert _size_thread_pool(cameras) >= cameras + 1

    def test_headroom_is_left_for_everything_that_is_not_a_camera(self):
        """API requests, DB commits and inference share this pool. A pool
        sized to exactly the camera count starves all of them."""
        assert _size_thread_pool(34) == 34 + settings.worker_thread_pool_headroom

    def test_a_small_deployment_is_not_shrunk_below_the_python_default(self):
        """Two cameras must not mean a 26-thread pool that is smaller than
        what the process had before this existed."""
        assert _size_thread_pool(2) >= 32
        assert _size_thread_pool(0) >= 32

    def test_a_large_catalog_is_capped(self):
        """A grid with a thousand cameras must not try to open a thousand
        threads; past the cap the honest answer is fewer connected cameras,
        not an unusable process."""
        assert _size_thread_pool(5000) == settings.worker_thread_pool_max

    def test_the_cap_is_above_any_plausible_single_machine_deployment(self):
        assert settings.worker_thread_pool_max >= 128


class TestExplicitSizing:
    def test_a_configured_size_wins(self, monkeypatch):
        monkeypatch.setattr(settings, "worker_thread_pool_size", 48)
        assert _size_thread_pool(34) == 48

    def test_a_configured_size_is_not_clamped_by_the_camera_count(self, monkeypatch):
        """An operator who pins the number means it — including below what
        the automatic formula would pick."""
        monkeypatch.setattr(settings, "worker_thread_pool_size", 8)
        assert _size_thread_pool(200) == 8

    def test_zero_means_automatic(self, monkeypatch):
        monkeypatch.setattr(settings, "worker_thread_pool_size", 0)
        assert _size_thread_pool(34) == 34 + settings.worker_thread_pool_headroom
