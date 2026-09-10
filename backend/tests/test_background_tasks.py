"""Regression: fire-and-forget work must survive to shutdown, not be destroyed.

Found by probing the real shutdown path: a pending `clips.build_event_clip`
was still running after `_on_shutdown()` returned, and was then destroyed when
the event loop closed. That task waits up to `clip_post_event_seconds` for
post-event frames BEFORE writing its Evidence row, so a shutdown inside that
window silently discarded evidence for a real alert — and produced
"Task was destroyed but it is pending" noise.

asyncio holds only a weak reference to a running task, so a bare `create_task`
whose result nobody keeps can also be collected mid-flight. `background.spawn`
holds a strong reference and lets shutdown drain the set.
"""
import asyncio

import pytest

from app import background


@pytest.fixture(autouse=True)
def _clean_registry():
    background._TASKS.clear()
    yield
    background._TASKS.clear()


def test_spawned_work_is_allowed_to_finish_before_shutdown_completes():
    finished: list[str] = []

    async def slow_evidence_write():
        await asyncio.sleep(0.2)
        finished.append("evidence written")

    async def scenario():
        background.spawn(slow_evidence_write(), name="clip:test")
        assert background.pending_count() == 1
        await background.drain(timeout=5.0)

    asyncio.run(scenario())
    assert finished == ["evidence written"], "shutdown must not discard in-flight evidence work"
    assert background.pending_count() == 0


def test_a_task_that_overruns_the_budget_is_cancelled_deliberately():
    """A clean exit matters more than one last clip — but the task must be
    cancelled and logged, not silently destroyed by the closing loop."""
    cancelled: list[str] = []

    async def never_finishes():
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            cancelled.append("cancelled")
            raise

    async def scenario():
        background.spawn(never_finishes(), name="stuck")
        await background.drain(timeout=0.1)
        # drain must not return until the cancellation has actually unwound.
        assert background.pending_count() == 0

    asyncio.run(scenario())
    assert cancelled == ["cancelled"]


def test_drain_is_a_no_op_when_nothing_is_pending():
    asyncio.run(background.drain(timeout=1.0))


def test_a_failing_background_task_never_breaks_shutdown():
    """One bad clip encode must not prevent the process exiting cleanly."""

    async def explodes():
        raise RuntimeError("encoder failed")

    async def scenario():
        background.spawn(explodes(), name="bad-clip")
        await background.drain(timeout=2.0)

    asyncio.run(scenario())  # must not raise


def test_completed_tasks_are_released_from_the_registry():
    """The registry is bounded by completion, not by shutdown — a long-running
    process must not accumulate every clip task it ever spawned."""

    async def scenario():
        for i in range(20):
            background.spawn(asyncio.sleep(0), name=f"t{i}")
        await background.drain(timeout=2.0)
        return len(background._TASKS)

    assert asyncio.run(scenario()) == 0


def test_the_real_shutdown_path_leaves_nothing_pending():
    """End-to-end against the actual `_on_shutdown`, which is where the defect
    was originally observed."""
    from app.main import _on_shutdown

    async def scenario():
        started = asyncio.Event()

        async def work():
            started.set()
            await asyncio.sleep(0.15)

        background.spawn(work(), name="clip:shutdown-check")
        await started.wait()
        await _on_shutdown()
        return [t for t in asyncio.all_tasks() if t is not asyncio.current_task() and not t.done()]

    assert asyncio.run(scenario()) == []
