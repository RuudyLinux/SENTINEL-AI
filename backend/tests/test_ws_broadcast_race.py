"""broadcast iterated self.active while awaiting each send_text. If another
task called manager.disconnect(ws) during that await (the per-client receive
loop does as soon as its socket drops), the list shifted and the next live
client could be skipped for that broadcast, CRITICAL alerts included.

Reproduced without real concurrency: a fake socket whose send_text mutates
the list is the same hazard.
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
        # while broadcast awaits A's send, A's own receive task notices the
        # disconnect and calls manager.disconnect(a)
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
    """Several clients disconnect at different positions across several
    broadcasts; everyone connected at the start of a broadcast either gets
    it or is the one leaving."""
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
