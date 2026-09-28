"""Audit logging with a tamper-evident hash chain.

entry_hash = sha256(prev_hash + the row's canonical fields), prev_hash is the
previous row's entry_hash ("0"*64 for the first). Edit or delete any row and
every hash after it breaks; verify_chain (GET /api/audit/verify-chain) looks
for that. Just hash chaining for tamper evidence on an append-only log, no
consensus or ledger.

chain_seq orders the chain (ids are random) and is unique, so a concurrent
writer raises IntegrityError and we retry against the new tail instead of
mis-ordering it.
"""
import hashlib
import logging

from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session

from . import models

logger = logging.getLogger("sentinel.audit")

GENESIS_HASH = "0" * 64
_MAX_CHAIN_RETRIES = 5

# Max length of `resource`. A failed login audits the submitted username, so
# this takes unauthenticated attacker text: a 5,000-char username made a
# 5,000-char row and the audit table (whitespace-nowrap) rendered 36,215px
# wide. The rate limiter bounds how many attempts, not how big each row is.
#
# Here and not in the login route because every audit write goes through this
# function. 512 is way above any real resource (uids are ~14 chars, the
# longest is demo_reset's camera list).
MAX_AUDIT_RESOURCE_CHARS = 512
_TRUNCATION_MARKER = "...[truncated]"


def _bounded_resource(resource: str) -> str:
    """Cap `resource` and mark it when shortened, so a reader can tell a
    cut value from a real one. The marker fits inside the cap."""
    if resource is None:
        return ""
    if len(resource) <= MAX_AUDIT_RESOURCE_CHARS:
        return resource
    return resource[: MAX_AUDIT_RESOURCE_CHARS - len(_TRUNCATION_MARKER)] + _TRUNCATION_MARKER


def _canonical_fields(entry: models.AuditLog) -> str:
    """Everything the hash covers, as one string. Field order is part of the
    contract, changing it changes every future hash."""
    return "|".join([
        str(entry.chain_seq), entry.id, entry.user_id or "", entry.username or "",
        entry.action or "", entry.resource or "", entry.result or "", entry.ip or "",
        entry.timestamp.isoformat() if entry.timestamp else "",
    ])


def compute_entry_hash(entry: models.AuditLog, prev_hash: str) -> str:
    return hashlib.sha256((prev_hash + "|" + _canonical_fields(entry)).encode("utf-8")).hexdigest()


def log_action(
    db: Session, user: "models.User | None", action: str, resource: str = "", result: str = "SUCCESS",
    ip: str = "", actor: "str | None" = None,
):
    """Insert an audit row and extend the chain.

    Retries on a chain_seq collision instead of corrupting the chain. If it
    still can't land a link after a few tries it logs and returns: a missed
    audit link must never block the operation being audited.
    """
    from datetime import datetime

    # cap first so the hashed value is exactly the stored value
    resource = _bounded_resource(resource)

    for attempt in range(_MAX_CHAIN_RETRIES):
        tail = db.query(models.AuditLog).order_by(models.AuditLog.chain_seq.desc()).first()
        prev_hash = tail.entry_hash if (tail is not None and tail.entry_hash) else GENESIS_HASH
        next_seq = (tail.chain_seq + 1) if (tail is not None and tail.chain_seq is not None) else 1

        entry = models.AuditLog(
            id=models.uid("aud"),  # set here, the column default only runs at flush, after the hash
            user_id=user.id if user else None,
            # actor names a non-user origin ("system"); "anonymous" is for
            # unauthenticated requests like a failed login
            username=user.username if user else (actor or "anonymous"),
            action=action, resource=resource, result=result, ip=ip,
            timestamp=datetime.utcnow(), chain_seq=next_seq, prev_hash=prev_hash,
        )
        entry.entry_hash = compute_entry_hash(entry, prev_hash)
        db.add(entry)
        try:
            db.commit()
            return entry
        except IntegrityError:
            # someone else took this chain_seq, roll back and retry on the new tail
            db.rollback()
            continue
        except OperationalError as exc:
            # A camera worker held the write lock past the busy timeout. This
            # used to escape as a 500 on whatever was being audited (upload,
            # ack, download). Retried like a collision; other errors raise.
            if "locked" not in str(exc).lower() and "busy" not in str(exc).lower():
                raise
            db.rollback()
            logger.warning("audit chain: database locked writing action=%s (attempt %d)", action, attempt + 1)
            continue
    logger.error("audit chain: could not land a consistent link after %d attempts for action=%s", _MAX_CHAIN_RETRIES, action)
    return None


def verify_chain(db: Session) -> dict:
    """Walk the chain and check every link. Returns {"valid", "checked",
    "broken_at", "detail"}. Rows from before the chain existed (chain_seq
    NULL) are reported as such, not skipped or counted as valid."""
    rows = db.query(models.AuditLog).order_by(models.AuditLog.chain_seq.asc()).all()
    unchained = [r for r in rows if r.chain_seq is None]
    chained = [r for r in rows if r.chain_seq is not None]

    prev_hash = GENESIS_HASH
    for row in chained:
        if row.prev_hash != prev_hash:
            return {
                "valid": False, "checked": len(chained), "broken_at": row.chain_seq,
                "detail": f"chain_seq {row.chain_seq}: prev_hash does not match the previous entry's entry_hash "
                          "— a row was likely deleted or reordered.",
                "pre_chain_rows": len(unchained),
            }
        expected = compute_entry_hash(row, prev_hash)
        if row.entry_hash != expected:
            return {
                "valid": False, "checked": len(chained), "broken_at": row.chain_seq,
                "detail": f"chain_seq {row.chain_seq}: entry_hash does not match its own recomputed hash "
                          "— this row's fields were modified after it was written.",
                "pre_chain_rows": len(unchained),
            }
        prev_hash = row.entry_hash

    return {
        "valid": True, "checked": len(chained), "broken_at": None,
        "detail": "Chain intact." if not unchained else (
            f"Chain intact for {len(chained)} chained rows; {len(unchained)} row(s) predate the hash chain "
            "and are not covered by it."
        ),
        "pre_chain_rows": len(unchained),
    }
