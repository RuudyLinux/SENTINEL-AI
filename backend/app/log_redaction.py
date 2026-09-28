"""Keep tokens out of server logs.

The /ws handshake, MJPEG/snapshot <img> tags and evidence downloads carry a
token in the URL (browsers can't add a header there), and uvicorn logs the
full path with query string, so JWTs sat in plain text in every log.

Attached to uvicorn's loggers at app import, so it works however uvicorn is
started instead of relying on --no-access-log.
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


# uvicorn.access has HTTP lines; the WebSocket handshake lines
# ("WebSocket /ws?token=..." [accepted]) go to uvicorn.error
REDACTED_LOGGERS = ("uvicorn.access", "uvicorn.error")


def install() -> None:
    for name in REDACTED_LOGGERS:
        logger = logging.getLogger(name)
        if not any(isinstance(f, TokenRedactingFilter) for f in logger.filters):
            logger.addFilter(TokenRedactingFilter())
