"""Registry for fire-and-forget background tasks, so shutdown can drain them.

Several places legitimately spawn work that the caller must not wait on — an
event-clip encode must not stall the camera loop that triggered it, and a
self-heal log entry must never delay the recovery it is describing. Those were
plain `asyncio.create_task` calls held by nothing.

The bug that produced this module: a pending `clips.build_event_clip` survived
`_on_shutdown` and was destroyed when the event loop closed (verified: two
tasks still pending after shutdown returned). That task waits up to
`clip_post_event_seconds` for post-event frames before writing its Evidence
row, so a shutdown during that window silently discarded evidence for a real
alert — and emitted "Task was destroyed but it is pending" noise.

Deliberately minimal: a set, a done-callback, and a bounded drain. This is not
a task queue and must not grow into one — nothing here needs durability across
processes, only a clean exit.
"""
import asyncio
import logging
from typing import Any, Coroutine

logger = logging.getLogger("sentinel.background")

_TASKS: "set[asyncio.Task[Any]]" = set()


def spawn(coro: "Coroutine[Any, Any, Any]", *, name: str) -> "asyncio.Task[Any]":
    """Start a background task that shutdown will wait for.

    The task holds a strong reference in `_TASKS` until it finishes. That is the
    point: asyncio only keeps a weak reference to a running task, so a bare
    `create_task` result that nobody stores can be garbage-collected mid-flight.
    """
    task = asyncio.create_task(coro, name=name)
    _TASKS.add(task)
    task.add_done_callback(_TASKS.discard)
    return task


def pending_count() -> int:
    return sum(1 for task in _TASKS if not task.done())


async def drain(timeout: float) -> None:
    """Let in-flight background work finish, then cancel whatever is left.

    Called from shutdown AFTER the camera workers have stopped, so nothing new
    is being spawned while this waits. Anything still running past `timeout` is
    cancelled — a clean exit matters more than one last clip — but it is
    cancelled deliberately and logged, rather than being destroyed silently by
    the closing loop.
    """
    pending = [task for task in _TASKS if not task.done()]
    if not pending:
        return
    logger.info("draining %d background task(s) before shutdown", len(pending))
    done, still_running = await asyncio.wait(pending, timeout=timeout)
    if still_running:
        logger.warning(
            "%d background task(s) did not finish within %.0fs — cancelling them",
            len(still_running), timeout,
        )
        for task in still_running:
            task.cancel()
        # gather, not bare cancel(): cancellation is only a REQUEST, and this
        # must not return until each task has actually unwound (the same
        # contract main.py's worker shutdown follows).
        await asyncio.gather(*still_running, return_exceptions=True)
    logger.info("background drain complete (%d finished, %d cancelled)", len(done), len(still_running))
