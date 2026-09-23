"""SHA-256 hashing for evidence files.

Lives in its own module because the hash must be taken at CAPTURE time, by the
pipeline that writes the file, while the comparison happens later in the
evidence API — and the two must use identical bytes-to-digest logic or every
comparison would be a false mismatch.

Why capture-time: the digest is only evidence of integrity if it was recorded
before anyone could alter the file. Hashing on first inspection instead — which
is what this system did — records whatever the file contains at that moment and
proves nothing about what was originally captured.
"""
import hashlib
import logging

logger = logging.getLogger("sentinel.evidence")

# Files are read in chunks rather than whole: an event clip is a real MP4, and
# reading one fully into memory to hash it would spike a process that is
# simultaneously decoding several camera streams.
_CHUNK_BYTES = 1024 * 1024


def sha256_file(path: str | None) -> str:
    """Digest of the file at `path`, or "" if it cannot be read.

    Never raises. Hashing is an integrity aid, not the operation itself — a
    snapshot that was genuinely captured must still be recorded as evidence
    even if hashing it fails, with an empty digest honestly showing that no
    baseline exists rather than a fabricated one.
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
