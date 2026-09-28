"""Concurrency cap on POST /api/cameras/test-connection.

Each probe holds a thread from the shared to_thread pool for up to
source_open_timeout_seconds (exactly 20.0s for loopback, 0.0.0.0,
link-local, IPv6, junk and file:// URIs). That pool is min(32, cpu+4) and
also runs every camera's reads, inference and commits, so a burst of probes
from an operator could starve live cameras just by using the endpoint.

Real ASGI transport, not the sync TestClient, so requests are actually
concurrent on one loop.
"""
import asyncio

import httpx
import pytest

from app.config import settings
from app.main import app
from app.routers import cameras as cameras_router


@pytest.fixture(autouse=True)
def _reset_semaphore():
    """The cap is process-global; rebuilt per test so limits don't leak."""
    cameras_router._probe_semaphore = None
    yield
    cameras_router._probe_semaphore = None


@pytest.fixture
def slow_source(monkeypatch):
    """open() blocks, like an unreachable RTSP host holding a thread for 20s,
    without the test taking 20s."""
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
    """Other work on the shared pool still finishes quickly while probes run."""
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
    """Slots come back; a leaking cap would break the endpoint after one burst."""
    monkeypatch.setattr(settings, "camera_test_connection_max_concurrent", 2)
    auth = {"Authorization": f"Bearer {admin_token}"}

    async def scenario():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            await asyncio.gather(*[_probe(client, auth) for _ in range(6)])
            # burst over, one probe gets through again
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
