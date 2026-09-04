"""Bounded retry/backoff for THIS APP'S OWN outbound HTTP calls — the "API
transient errors" recovery type (Self-Heal spec recovery type 6). Applied to
pipeline/catalog.py's camera-catalogue fetch.

Retries only the standard transient set (408/429/500/502/503/504) plus a
network-level timeout/connection failure, bounded exponential backoff, a
hard max-attempts ceiling — never retried forever. Anything else (400, 401,
403, 404, 422, or any other status) is returned as-is / re-raised
immediately: a bad request or bad credentials retried blindly just hammers
the remote endpoint for no new information.

Deliberately NOT wired into pipeline/sentinel_grid.py: that integration's
login + fetch timeouts were tuned against real, measured round-trips to the
live grid (see config.py's sentinel_grid_timeout_seconds/
source_open_timeout_seconds comments) and is explicitly "already working,
do not break it" per the task brief — adding a second retry layer on top of
already-tuned timeouts risks changing its real, verified behavior for no
proven benefit.
"""
import asyncio
import logging
from typing import Awaitable, Callable

import httpx

logger = logging.getLogger("sentinel.self_heal")

RETRYABLE_STATUS = {408, 429, 500, 502, 503, 504}
NON_RETRYABLE_STATUS = {400, 401, 403, 404, 422}


async def request_with_retry(
    do_request: Callable[[], Awaitable[httpx.Response]],
    label: str,
    max_attempts: int = 3,
    backoff_base: float = 0.5,
) -> httpx.Response:
    """`do_request` performs ONE httpx call (e.g. `lambda: client.get(url)`)
    — a callback rather than this function owning the request, so callers
    keep full control (headers/auth/client) and this stays a thin, reusable
    wrapper. Returns the final response (which may still carry a
    non-retryable error status — callers keep their own status-code
    handling unchanged) or re-raises the last network-level exception after
    exhausting max_attempts."""
    last_exc: Exception | None = None
    resp: httpx.Response | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            resp = await do_request()
        except (httpx.TimeoutException, httpx.ConnectError, httpx.ReadError) as exc:
            last_exc = exc
            if attempt >= max_attempts:
                raise
            logger.warning(
                "%s: transient network error (attempt %d/%d): %s — retrying",
                label, attempt, max_attempts, exc,
            )
        else:
            if resp.status_code not in RETRYABLE_STATUS or attempt >= max_attempts:
                return resp
            logger.warning("%s: HTTP %d (attempt %d/%d) — retrying", label, resp.status_code, attempt, max_attempts)
        await asyncio.sleep(backoff_base * (2 ** (attempt - 1)))
    if resp is not None:
        return resp
    assert last_exc is not None  # loop always either returns or sets last_exc before falling through
    raise last_exc
