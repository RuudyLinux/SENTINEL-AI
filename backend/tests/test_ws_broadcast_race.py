"""BUG-2 (found in the 10/10 debugging pass, 2026-09-11):
`ConnectionManager.broadcast` iterated `self.active` directly while
`await`ing each client's `send_text` — a real yield point. If a DIFFERENT
task calls `manager.disconnect(ws)` during that await (exactly what
`main.py::websocket_endpoint`'s own per-client receive-loop does the instant
it notices its socket disconnected), the list shifts under the iterator and
the NEXT still-connected, still-live client in the list can be silently
skipped for that one broadcast — including a CRITICAL `alert.created` event.

Reproduced deterministically (no real concurrency needed — a plain
synchronous side effect inside one fake socket's `send_text` reproduces the
exact list-mutation-during-iteration hazard, since Python's iterator
protocol does not care WHY the list changed mid-loop).
"""
import asyncio
import json

from app.ws import ConnectionManager


class _FakeSocket:
    def __init__(self):
        self.sent: list[dict] = []

    async def send_text(self, message: str) -> None:
        self.sent.append(json.loads(message))


def test_a_client_disconnecting_during_broadcast_does_not_skip_the_next_live_client():
    manager = ConnectionManager()
    a, b, c = _FakeSocket(), _FakeSocket(), _FakeSocket()
    manager.active.extend([a, b, c])

    async def a_send_text(_message: str) -> None:
        # Simulate the real interleaving: while broadcast() is awaiting A's
        # send, A's OWN receive-loop task (main.py::websocket_endpoint,
        # a separate asyncio task per connected client) notices A's socket
        # disconnected and calls manager.disconnect(a) concurrently.
        manager.disconnect(a)

    a.send_text = a_send_text  # type: ignore[method-assign]

    asyncio.run(manager.broadcast("test.event", {"x": 1}))

    assert len(b.sent) == 1, (
        "client B was still connected when broadcast() started but received nothing — "
        "it was silently skipped because A's mid-broadcast disconnect shifted the list "
        "broadcast() was iterating directly."
    )
    assert len(c.sent) == 1
    assert a not in manager.active
    assert b in manager.active
    assert c in manager.active


def test_broadcast_still_reaches_every_client_under_repeated_churn():
    """Stronger version: several clients disconnect themselves mid-broadcast,
    at different positions in the list, across several broadcasts — the
    invariant (every client connected AT THE START of a given broadcast call
    either receives it or was the one disconnecting) must hold regardless of
    WHICH position churns."""
    manager = ConnectionManager()
    sockets = [_FakeSocket() for _ in range(6)]
    manager.active.extend(sockets)

    # Every other socket disconnects itself the moment it is sent to.
    def _make_self_disconnecting_send(sock):
        async def _send(_message: str) -> None:
            manager.disconnect(sock)
        return _send

    for i, sock in enumerate(sockets):
        if i % 2 == 0:
            sock.send_text = _make_self_disconnecting_send(sock)  # type: ignore[method-assign]

    asyncio.run(manager.broadcast("test.event", {"x": 1}))

    still_connected = [s for i, s in enumerate(sockets) if i % 2 == 1]
    for sock in still_connected:
        assert len(sock.sent) == 1, "a live client was skipped during broadcast churn"
