"""/ws needs a valid token; it used to accept anyone and hand out live
detection/alert events. Browsers can't set a header on the handshake, so the
token is a query param, like the evidence/stream tokens."""
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
    # a valid token gets an open connection; a rejected handshake raises
    # WebSocketDisconnect(4401) on entry instead
    with client.websocket_connect(f"/ws?token={admin_token}"):
        pass
