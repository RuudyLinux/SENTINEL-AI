"""Audit logging with a tamper-evident hash chain (10/10 roadmap P9).

Each row's `entry_hash` is sha256(prev_hash + this row's own canonical
fields); `prev_hash` is the entry_hash of the row before it in the chain
(the genesis row uses "0"*64). Deleting or editing ANY row breaks every
`entry_hash` computed after it — that propagating break, not any single row
in isolation, is what `verify_chain` in this module (and
`GET /api/audit/verify-chain`) detects. This is deliberately NOT
"blockchain" — no consensus, no mining, no distributed ledger — just the
same hash-chaining primitive blockchains use for the one property this
system actually needs: tamper evidence on an append-only log.

`chain_seq` orders the chain (AuditLog.id is a random uid, not insertion-
ordered) and is UNIQUE, so a concurrent-write race that would otherwise
silently mis-order the chain instead raises an IntegrityError, which is
retried against a freshly-read tail rather than swallowed.
"""
import hashlib
import logging

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from . import models

logger = logging.getLogger("sentinel.audit")

GENESIS_HASH = "0" * 64
_MAX_CHAIN_RETRIES = 5


def _canonical_fields(entry: models.AuditLog) -> str:
    """Deterministic string of everything the hash must cover. Field ORDER is
    part of the contract — changing it changes every future hash, so this is
    intentionally simple and stable rather than a generic serializer."""
    return "|".join([
        str(entry.chain_seq), entry.id, entry.user_id or "", entry.username or "",
        entry.action or "", entry.resource or "", entry.result or "", entry.ip or "",
        entry.timestamp.isoformat() if entry.timestamp else "",
    ])


def compute_entry_hash(entry: models.AuditLog, prev_hash: str) -> str:
    return hashlib.sha256((prev_hash + "|" + _canonical_fields(entry)).encode("utf-8")).hexdigest()


def log_action(db: Session, user: "models.User | None", action: str, resource: str = "", result: str = "SUCCESS", ip: str = ""):
    """Insert one audit row and extend the hash chain.

    Best-effort against races the same way SelfHealEvent is (see models.py):
    under genuinely concurrent writers this retries on a chain_seq collision
    rather than silently corrupting the chain, but is not a distributed
    consensus mechanism — if it cannot land a consistent link within a few
    attempts it logs the failure and still returns, because a missed audit
    LINK must never block the real operation it is describing.
    """
    from datetime import datetime

    for attempt in range(_MAX_CHAIN_RETRIES):
        tail = db.query(models.AuditLog).order_by(models.AuditLog.chain_seq.desc()).first()
        prev_hash = tail.entry_hash if (tail is not None and tail.entry_hash) else GENESIS_HASH
        next_seq = (tail.chain_seq + 1) if (tail is not None and tail.chain_seq is not None) else 1

        entry = models.AuditLog(
            id=models.uid("aud"),  # generated explicitly (not left to the Column default) so it is
            # known BEFORE the hash is computed — a Python-side Column default is only
            # evaluated by SQLAlchemy at flush time, too late for compute_entry_hash below.
            user_id=user.id if user else None,
            username=user.username if user else "anonymous",
            action=action, resource=resource, result=result, ip=ip,
            timestamp=datetime.utcnow(), chain_seq=next_seq, prev_hash=prev_hash,
        )
        entry.entry_hash = compute_entry_hash(entry, prev_hash)
        db.add(entry)
        try:
            db.commit()
            return entry
        except IntegrityError:
            # Another writer took this chain_seq between our read and our
            # commit — roll back and retry against the now-current tail.
            db.rollback()
            continue
    logger.error("audit chain: could not land a consistent link after %d attempts for action=%s", _MAX_CHAIN_RETRIES, action)
    return None


def verify_chain(db: Session) -> dict:
    """Walk the full chain in order and confirm every link. Returns
    {"valid": bool, "checked": N, "broken_at": chain_seq | None, "detail": str}.
    A missing baseline (rows written before this feature existed, chain_seq
    NULL) is reported honestly rather than silently skipped or treated as
    valid — those rows predate the chain and cannot be verified by it."""
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
