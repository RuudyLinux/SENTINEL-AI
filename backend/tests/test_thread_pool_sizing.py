"""The shared thread pool has to know how many cameras there are.

to_thread uses the loop's default executor, and here that one pool runs
every blocking call: camera reads, inference, DB commits, hashing, probes.
Python sizes it min(32, cpu_count + 4) once, 20 on a 16-core box, however
many cameras exist. With 34 cameras connected the online count bounced (12,
then 4) with last_error: none everywhere. Nothing failed; reads were queued
for a thread, which looks exactly like waiting for the camera.
"""
from app.config import settings
from app.main import _size_thread_pool


class TestAutomaticSizing:
    def test_every_camera_can_hold_a_thread_at_once(self):
        """The property that matters: cameras + headroom, never fewer."""
        for cameras in (34, 60, 100):
            assert _size_thread_pool(cameras) >= cameras + 1

    def test_headroom_is_left_for_everything_that_is_not_a_camera(self):
        """API, DB and inference share this pool; exactly one thread per
        camera starves them."""
        assert _size_thread_pool(34) == 2 * 34 + settings.worker_thread_pool_headroom

    def test_a_small_deployment_is_not_shrunk_below_the_python_default(self):
        """Two cameras mustn't mean a smaller pool than before this existed."""
        assert _size_thread_pool(2) >= 32
        assert _size_thread_pool(0) >= 32

    def test_a_large_catalog_is_capped(self):
        """A thousand cameras doesn't mean a thousand threads; past the cap
        you get fewer connected cameras, not an unusable process."""
        assert _size_thread_pool(5000) == settings.worker_thread_pool_max

    def test_the_cap_is_above_any_plausible_single_machine_deployment(self):
        assert settings.worker_thread_pool_max >= 128


class TestExplicitSizing:
    def test_a_configured_size_wins(self, monkeypatch):
        monkeypatch.setattr(settings, "worker_thread_pool_size", 48)
        assert _size_thread_pool(34) == 48

    def test_a_configured_size_is_not_clamped_by_the_camera_count(self, monkeypatch):
        """A pinned number is used as is, even below the automatic one."""
        monkeypatch.setattr(settings, "worker_thread_pool_size", 8)
        assert _size_thread_pool(200) == 8

    def test_zero_means_automatic(self, monkeypatch):
        monkeypatch.setattr(settings, "worker_thread_pool_size", 0)
        assert _size_thread_pool(34) == 2 * 34 + settings.worker_thread_pool_headroom
