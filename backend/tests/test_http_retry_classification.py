"""Final deep-debug pass — retry-classification audit for
`self_heal/http_retry.py`.

This module decides which outbound-HTTP failures are retried and which are
not, and it had NO test coverage at all before this pass (verified: nothing
under tests/ referenced `request_with_retry` or `http_retry`). That is
exactly the logic the debugging brief asks to prove — "make sure
non-retryable failures do not enter infinite retry loops" — so each case
below counts the REAL number of attempts made rather than trusting the
docstring.

Result: no bug found. Every case behaves as documented — recorded here so a
future change to the classifier cannot silently start hammering a remote
endpoint on a 401, or silently stop retrying a real 503.
"""
import asyncio

import httpx
import pytest

from app.self_heal.http_retry import RETRYABLE_STATUS, request_with_retry


def _responder(statuses):
    """Returns (callback, calls) where the callback yields the next status
    each time it is invoked, so a test can assert the ATTEMPT COUNT."""
    calls = []

    async def do_request() -> httpx.Response:
        status = statuses[min(len(calls), len(statuses) - 1)]
        calls.append(status)
        return httpx.Response(status_code=status, request=httpx.Request("GET", "https://example.invalid/x"))

    return do_request, calls


def _raiser(exc: Exception):
    calls = []

    async def do_request() -> httpx.Response:
        calls.append(1)
        raise exc

    return do_request, calls


class TestNonRetryableStatuses:
    @pytest.mark.parametrize("status", [400, 401, 403, 404, 422, 418])
    def test_a_non_retryable_status_is_returned_after_exactly_one_attempt(self, status):
        """A bad request or bad credentials retried blindly is just hammering
        the remote endpoint for no new information — and on an auth failure
        it is how you get an account locked out or an IP blocked."""
        do_request, calls = _responder([status])
        resp = asyncio.run(request_with_retry(do_request, label="probe", max_attempts=3))
        assert resp.status_code == status
        assert len(calls) == 1, f"HTTP {status} was retried {len(calls)} times — it must not be retried at all"


class TestRetryableStatuses:
    @pytest.mark.parametrize("status", sorted(RETRYABLE_STATUS))
    def test_a_retryable_status_is_retried_up_to_the_ceiling_then_given_up_on(self, status):
        do_request, calls = _responder([status])
        resp = asyncio.run(request_with_retry(do_request, label="probe", max_attempts=3, backoff_base=0.001))
        assert resp.status_code == status
        assert len(calls) == 3, f"HTTP {status} made {len(calls)} attempts, expected exactly the 3-attempt ceiling"

    def test_a_transient_failure_that_recovers_stops_retrying_immediately(self):
        do_request, calls = _responder([503, 200])
        resp = asyncio.run(request_with_retry(do_request, label="probe", max_attempts=5, backoff_base=0.001))
        assert resp.status_code == 200
        assert len(calls) == 2, "retrying continued after the call had already succeeded"


class TestNetworkExceptions:
    @pytest.mark.parametrize("exc", [
        httpx.TimeoutException("timed out"),
        httpx.ConnectError("connection refused"),
        httpx.ReadError("connection reset"),
    ])
    def test_transient_network_errors_are_retried_then_re_raised(self, exc):
        do_request, calls = _raiser(exc)
        with pytest.raises(type(exc)):
            asyncio.run(request_with_retry(do_request, label="probe", max_attempts=3, backoff_base=0.001))
        assert len(calls) == 3, f"expected the 3-attempt ceiling, got {len(calls)}"

    def test_an_unexpected_exception_is_not_retried_at_all(self):
        """Only the named transient network errors are retryable. A
        programming error (or any other exception) must surface immediately,
        not be retried as if it were a network blip."""
        do_request, calls = _raiser(ValueError("a bug, not a network problem"))
        with pytest.raises(ValueError):
            asyncio.run(request_with_retry(do_request, label="probe", max_attempts=3, backoff_base=0.001))
        assert len(calls) == 1, "a non-network exception was retried"


class TestBoundedness:
    def test_retrying_is_always_bounded_and_backs_off(self):
        """The core anti-infinite-loop property: a permanently-failing
        endpoint must terminate, and must not busy-loop while doing so."""
        import time

        do_request, calls = _responder([500])
        started = time.monotonic()
        asyncio.run(request_with_retry(do_request, label="probe", max_attempts=4, backoff_base=0.01))
        elapsed = time.monotonic() - started

        assert len(calls) == 4
        # 0.01 + 0.02 + 0.04 = 0.07s of backoff minimum — proves it slept
        # between attempts rather than spinning.
        assert elapsed >= 0.05, "no backoff observed between retries — a failing endpoint would be hammered"
        assert elapsed < 5.0, "backoff grew unreasonably for a small attempt ceiling"
