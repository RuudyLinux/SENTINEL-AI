"""Workstream C1 (final deep-debug pass): per-endpoint concurrency cap on
POST /api/cameras/test-connection.

The measured problem: every probe occupies a thread from the SHARED
`asyncio.to_thread` executor for up to `source_open_timeout_seconds` —
measured at exactly 20.0s each for loopback / 0.0.0.0 / link-local / IPv6 /
malformed / `file://` URIs. That executor is bounded
(min(32, cpu_count + 4) workers) and is the same pool every camera worker
uses for frame reads, inference offloads and DB commits. An
authorized-but-lower-trust Control Room Operator could therefore fire a
burst of probes and starve live camera processing for the full timeout,
without exploiting anything — just by using the endpoint.

Uses a real ASGI transport rather than the sync TestClient so the requests
are genuinely concurrent on one event loop, which is the condition under
test.
"""
import asyncio

import httpx
import pytest

from app.config import settings
from app.main import app
from app.routers import cameras as cameras_router


@pytest.fixture(autouse=True)
def _reset_semaphore():
    """The cap is process-global; rebuild it per test so a test's own limit
    change takes effect and cannot leak into the next test."""
    cameras_router._probe_semaphore = None
    yield
    cameras_router._probe_semaphore = None


@pytest.fixture
def slow_source(monkeypatch):
    """A source whose open() blocks, standing in for the measured real
    behavior (an unreachable RTSP endpoint holding a thread for 20s) without
    making the test itself take 20 seconds."""
    class _SlowSource:
        def __init__(self, source_type, source_uri):
            pass

        def open(self):
            import time
            time.sleep(0.6)
            return False

        def read(self):
            return False, None

        def release(self):
            return None

    monkeypatch.setattr(cameras_router, "CameraSource", _SlowSource)


async def _probe(client, auth):
    return await client.post(
        "/api/cameras/test-connection",
        data={"source_type": "rtsp", "source_uri": "rtsp://127.0.0.1:9/stream"},
        headers=auth,
    )


def test_probes_beyond_the_cap_are_refused_fast_instead_of_queueing(admin_token, slow_source, monkeypatch):
    monkeypatch.setattr(settings, "camera_test_connection_max_concurrent", 2)
    auth = {"Authorization": f"Bearer {admin_token}"}

    async def scenario():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            results = await asyncio.gather(*[_probe(client, auth) for _ in range(6)])
            return [r.status_code for r in results]

    statuses = asyncio.run(scenario())
    accepted = [s for s in statuses if s == 200]
    refused = [s for s in statuses if s == 429]

    assert refused, "no probe was refused — the concurrency cap is not enforced"
    assert len(accepted) <= 2, f"more probes ran concurrently than the cap allowed: {statuses}"
    assert len(accepted) + len(refused) == 6, f"unexpected statuses: {statuses}"


def test_the_shared_thread_pool_is_not_starved_by_a_probe_burst(admin_token, slow_source, monkeypatch):
    """The actual property that matters: unrelated work that also needs the
    shared executor must still complete promptly while probes are in
    flight."""
    monkeypatch.setattr(settings, "camera_test_connection_max_concurrent", 2)
    auth = {"Authorization": f"Bearer {admin_token}"}

    async def scenario():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            burst = [asyncio.create_task(_probe(client, auth)) for _ in range(12)]
            await asyncio.sleep(0.05)  # let the burst engage the cap

            # Unrelated executor-backed work, timed while probes are running.
            started = asyncio.get_event_loop().time()
            done = await asyncio.to_thread(lambda: sum(range(1000)))
            elapsed = asyncio.get_event_loop().time() - started

            await asyncio.gather(*burst)
            return done, elapsed

    done, elapsed = asyncio.run(scenario())
    assert done == 499500
    assert elapsed < 2.0, (
        f"unrelated to_thread work waited {elapsed:.2f}s behind a probe burst — "
        "the shared executor is still being starved"
    )


def test_the_cap_releases_so_later_probes_still_work(admin_token, slow_source, monkeypatch):
    """A cap that leaked its slots would permanently break the endpoint after
    one burst — worse than the problem it fixes."""
    monkeypatch.setattr(settings, "camera_test_connection_max_concurrent", 2)
    auth = {"Authorization": f"Bearer {admin_token}"}

    async def scenario():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            await asyncio.gather(*[_probe(client, auth) for _ in range(6)])
            # Burst over — a single probe must be accepted again.
            return (await _probe(client, auth)).status_code

    assert asyncio.run(scenario()) == 200


def test_the_cap_does_not_change_a_normal_single_probe(admin_token, slow_source):
    auth = {"Authorization": f"Bearer {admin_token}"}

    async def scenario():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await _probe(client, auth)
            return resp.status_code, resp.json()

    status, body = asyncio.run(scenario())
    assert status == 200
    assert body["ok"] is False  # the slow source reports failure, as before
    assert "detail" in body
