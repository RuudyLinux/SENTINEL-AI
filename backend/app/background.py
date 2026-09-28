"""Registry of fire-and-forget tasks so shutdown can drain them.

A clip encode mustn't stall the camera loop and a self-heal log write
mustn't delay the recovery it describes, so they're spawned and not
awaited. As bare create_task calls nothing held them: a pending
build_event_clip got destroyed when the loop closed at shutdown, losing
evidence for a real alert (and "Task was destroyed but it is pending").

Just a set, a done callback and a bounded drain. Not a task queue.
"""
import asyncio
import logging
from typing import Any, Coroutine

logger = logging.getLogger("sentinel.background")

_TASKS: "set[asyncio.Task[Any]]" = set()


def spawn(coro: "Coroutine[Any, Any, Any]", *, name: str) -> "asyncio.Task[Any]":
    """Start a task shutdown will wait for. _TASKS holds a strong reference
    until it finishes; asyncio only keeps a weak one, so an unstored task can
    be garbage-collected mid-flight."""
    task = asyncio.create_task(coro, name=name)
    _TASKS.add(task)
    task.add_done_callback(_TASKS.discard)
    return task


def pending_count() -> int:
    return sum(1 for task in _TASKS if not task.done())


async def drain(timeout: float) -> None:
    """Let running tasks finish, then cancel the rest. Called after the
    camera workers are stopped, so nothing new gets spawned. Past `timeout`
    things are cancelled and logged, a clean exit beats one last clip."""
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
        # gather, not just cancel(): wait until each task has actually unwound
        await asyncio.gather(*still_running, return_exceptions=True)
    logger.info("background drain complete (%d finished, %d cancelled)", len(done), len(still_running))
