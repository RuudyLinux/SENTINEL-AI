"""SHA-256 for evidence files.

Computed at capture by the pipeline and compared later by the evidence API, so
both share this logic. A digest only proves integrity if it was recorded before
anyone could change the file.
"""
import hashlib
import logging

logger = logging.getLogger("sentinel.evidence")

# chunked, clips are real MP4s and this process is decoding several streams
_CHUNK_BYTES = 1024 * 1024


def sha256_file(path: str | None) -> str:
    """Digest of the file, or "" if unreadable. Never raises: a snapshot that
    was really captured still gets recorded, with an empty digest showing
    there's no baseline.
    """
    if not path:
        return ""
    digest = hashlib.sha256()
    try:
        with open(path, "rb") as handle:
            for chunk in iter(lambda: handle.read(_CHUNK_BYTES), b""):
                digest.update(chunk)
    except OSError:
        logger.exception("could not hash evidence file %s — storing no baseline", path)
        return ""
    return digest.hexdigest()
