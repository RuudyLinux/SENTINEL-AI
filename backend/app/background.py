"""Registry of fire-and-forget tasks (clip encodes, self-heal writes) so
shutdown can drain them instead of destroying them mid-flight.
"""
import asyncio
import logging
from typing import Any, Coroutine

logger = logging.getLogger("sentinel.background")

_TASKS: "set[asyncio.Task[Any]]" = set()


def spawn(coro: "Coroutine[Any, Any, Any]", *, name: str) -> "asyncio.Task[Any]":
    """Start a task that shutdown will wait for. A strong reference is held until
    it finishes; asyncio keeps only a weak one."""
    task = asyncio.create_task(coro, name=name)
    _TASKS.add(task)
    task.add_done_callback(_TASKS.discard)
    return task


def pending_count() -> int:
    return sum(1 for task in _TASKS if not task.done())


async def drain(timeout: float) -> None:
    """Let running tasks finish, then cancel the rest after `timeout`. Called
    once camera workers have stopped."""
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
