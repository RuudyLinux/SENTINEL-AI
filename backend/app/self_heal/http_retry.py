"""Bounded retry with backoff for our own outbound HTTP (the catalogue fetch).

Retries 408/429/500/502/503/504 and network timeouts/connection errors,
exponential backoff, hard max attempts. Anything else (400, 401, 403, 404,
422...) comes straight back; retrying bad requests or bad credentials just
hammers the other end.

Not used for sentinel_grid.py: its timeouts were tuned against real round
trips to the grid and it works, a second retry layer on top could change that.
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
    """do_request makes ONE httpx call (e.g. lambda: client.get(url)), so
    callers keep control of headers/auth/client. Returns the last response
    (may still be an error status, callers handle those) or re-raises the
    last network error after max_attempts."""
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
    assert last_exc is not None  # the loop returns or sets last_exc
    raise last_exc
