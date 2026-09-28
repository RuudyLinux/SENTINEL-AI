"""Tokens in URLs must never reach a log line (see app/log_redaction.py)."""
import logging

from app import log_redaction, main  # noqa: F401 - importing main installs the filter


def _render(logger_name: str, msg: str, *args) -> str:
    logger = logging.getLogger(logger_name)
    record = logger.makeRecord(logger_name, logging.INFO, __file__, 1, msg, args, None)
    for f in logger.filters:
        f.filter(record)
    return record.getMessage()


SECRET = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ1c3IifQ.c2lnbmF0dXJl"


def test_http_access_line_is_redacted():
    # uvicorn.access formats: client, method, full path, http version, status
    line = _render("uvicorn.access", '%s - "%s %s HTTP/%s" %d',
                   "127.0.0.1:1", "GET", f"/api/evidence/e1/file?token={SECRET}&x=1", "1.1", 200)
    assert SECRET not in line
    assert "token=[REDACTED]&x=1" in line


def test_websocket_handshake_line_is_redacted():
    # uvicorn logs WebSocket handshakes on uvicorn.error, not uvicorn.access
    line = _render("uvicorn.error", '%s - "WebSocket %s" [accepted]', "127.0.0.1:1", f"/ws?token={SECRET}")
    assert SECRET not in line and "/ws?token=[REDACTED]" in line


def test_stream_url_with_extra_params_is_redacted():
    line = _render("uvicorn.access", "%s", f"/api/streams/c/mjpeg?token={SECRET}&reconnect=1")
    assert SECRET not in line and line.endswith("token=[REDACTED]&reconnect=1")


def test_lines_without_tokens_are_untouched():
    assert _render("uvicorn.access", "%s", "/api/cameras?include_retired=true") == "/api/cameras?include_retired=true"


def test_install_is_idempotent():
    log_redaction.install()
    log_redaction.install()
    for name in log_redaction.REDACTED_LOGGERS:
        filters = [f for f in logging.getLogger(name).filters if isinstance(f, log_redaction.TokenRedactingFilter)]
        assert len(filters) == 1
