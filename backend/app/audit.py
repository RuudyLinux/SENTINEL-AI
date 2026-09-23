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

# Upper bound on the `resource` a single audit row may store.
#
# Real, reachable case: `POST /api/auth/login` audits a failed attempt with the
# SUBMITTED username as the resource, so this column takes attacker-controlled
# text with no account and no credential behind it. Measured against the
# running system before this bound existed — a 5,000-character username stored
# a 5,000-character row, and the audit page (every cell `whitespace-nowrap`)
# rendered a table 36,215px wide, making the compliance screen unusable. The
# login rate limiter bounds how MANY attempts are made; nothing bounded how
# LARGE each one's audit row was.
#
# Applied here rather than at the login call site because this function is the
# single funnel every audit write passes through, so the bound covers callers
# that do not exist yet — and the login path is only the one that happens to be
# reachable without credentials, not the only one taking free text.
#
# 512 is far above every identifier the system actually records (a uid is ~14
# characters, the longest real resource is a comma-joined camera list from
# demo_reset) and far below anything that can distort a table or a datastore.
MAX_AUDIT_RESOURCE_CHARS = 512
_TRUNCATION_MARKER = "...[truncated]"


def _bounded_resource(resource: str) -> str:
    """Bound `resource`, and SAY SO when it was shortened.

    A silent cut would make the audit trail quietly disagree with what was
    actually submitted, which is worse than a shortened value: a reader of the
    log could not tell a 512-character resource from a 5,000-character one.
    The marker is inside the bound, so the stored string never exceeds it.
    """
    if resource is None:
        return ""
    if len(resource) <= MAX_AUDIT_RESOURCE_CHARS:
        return resource
    return resource[: MAX_AUDIT_RESOURCE_CHARS - len(_TRUNCATION_MARKER)] + _TRUNCATION_MARKER


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

    # Bounded before the entry is built, so the value that is hashed is the
    # value that is stored — the chain covers exactly what the row contains.
    resource = _bounded_resource(resource)

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
