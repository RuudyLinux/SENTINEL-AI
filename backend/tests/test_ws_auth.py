"""/ws real-time event feed (detection/alert broadcasts, worker.py /
rules_engine.py) requires a valid access token — hardening-pass finding:
this endpoint previously accepted any connection with no authentication
at all, handing out live surveillance events to anyone who could reach
the backend. Browsers can't attach an Authorization header to a
WebSocket handshake, so the token travels as a query param instead
(mirrors the existing evidence/stream resource-token pattern)."""
from starlette.websockets import WebSocketDisconnect


def test_ws_rejects_connection_with_no_token(client):
    try:
        with client.websocket_connect("/ws") as ws:
            ws.receive_text()
        assert False, "expected the handshake to be rejected"
    except WebSocketDisconnect as exc:
        assert exc.code == 4401


def test_ws_rejects_connection_with_invalid_token(client):
    try:
        with client.websocket_connect("/ws?token=not-a-real-jwt") as ws:
            ws.receive_text()
        assert False, "expected the handshake to be rejected"
    except WebSocketDisconnect as exc:
        assert exc.code == 4401


def test_ws_accepts_connection_with_a_valid_token(client, admin_token):
    # A valid token gets a real, open connection — proven by the connection
    # not raising on entry/exit of the context manager (a rejected handshake
    # raises WebSocketDisconnect(4401) instead, per the two tests above).
    with client.websocket_connect(f"/ws?token={admin_token}"):
        pass
