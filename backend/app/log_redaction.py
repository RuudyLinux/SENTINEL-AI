"""Keep credentials out of server logs.

Three kinds of request carry a token in the URL because a browser cannot attach
an Authorization header to them: the /ws handshake, MJPEG/snapshot <img> tags,
and evidence/package download links. uvicorn logs the full path with query
string for every request and every WebSocket handshake, so a session JWT (8h)
or a resource token sat in plain text in every server log.

The filter is attached to uvicorn's own loggers at import time of the app, so
it holds however uvicorn is launched — start.bat, Docker, or a bare command —
instead of depending on an operator remembering --no-access-log.
"""
import logging
import re

_TOKEN_QUERY = re.compile(r"((?:^|[?&])(?:token|access_token)=)[^&\s\"']+", re.IGNORECASE)
REDACTED = "[REDACTED]"


def redact(text: str) -> str:
    return _TOKEN_QUERY.sub(r"\1" + REDACTED, text)


class TokenRedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = redact(record.msg)
        if isinstance(record.args, tuple):
            record.args = tuple(redact(a) if isinstance(a, str) else a for a in record.args)
        elif isinstance(record.args, dict):
            record.args = {k: redact(v) if isinstance(v, str) else v for k, v in record.args.items()}
        return True


# uvicorn.access: HTTP request lines. uvicorn.error: WebSocket handshake lines
# ("WebSocket /ws?token=..." [accepted]) are logged here, not on the access logger.
REDACTED_LOGGERS = ("uvicorn.access", "uvicorn.error")


def install() -> None:
    for name in REDACTED_LOGGERS:
        logger = logging.getLogger(name)
        if not any(isinstance(f, TokenRedactingFilter) for f in logger.filters):
            logger.addFilter(TokenRedactingFilter())
