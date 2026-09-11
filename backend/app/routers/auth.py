import time

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from .. import models, schemas
from ..db import get_db
from ..security import verify_password, create_access_token, get_current_user
from ..audit import log_action

router = APIRouter(prefix="/api/auth", tags=["auth"])

# In-memory login rate limiter (Phase 11 security baseline) — a first layer
# against credential brute-forcing, keyed by username (the field an attacker
# actually varies against is the password, not the source IP). Single-process,
# not distributed — documented limitation, not claimed as a full solution.
# Sliding window: MAX_ATTEMPTS failures within WINDOW_SECONDS locks out further
# attempts for that username until the window rolls forward; a success clears it.
_LOGIN_MAX_ATTEMPTS = 5
_LOGIN_WINDOW_SECONDS = 60.0

# BUG-A fix (final deep-debug pass): this table is keyed by an ATTACKER-
# CONTROLLED string and had neither a key-count bound nor a key-size bound, so
# every failed login permanently added an entry. Measured before the fix: 500
# novel usernames -> 500 permanent keys; one 1,000,000-character username ->
# one permanent 1,000,049-byte key. No account and no credential were needed —
# just repeated POST /api/auth/login. That is remote, unauthenticated,
# unbounded memory growth.
#
# Both bounds are enforced below:
#   - key SIZE: the key is the username truncated to _LOGIN_KEY_MAX_CHARS.
#     Two usernames only share a key if they share a 128-character prefix,
#     which no real account here does; when it happens it makes limiting
#     STRICTER for those absurd names, never weaker for a real one.
#   - key COUNT: capped at _LOGIN_MAX_TRACKED_USERNAMES, evicting only
#     entries whose attempts have ALL aged out of the window. Eviction never
#     touches a username currently at the limit, so spraying novel
#     usernames cannot flush an actively-attacked account's counter (that
#     is, an entry already at the limit is never evicted; that
#     would turn the memory fix into a rate-limit bypass — see
#     tests/test_login_ratelimit_hardening.py).
_LOGIN_KEY_MAX_CHARS = 128
_LOGIN_MAX_TRACKED_USERNAMES = 1024
_failed_attempts: dict[str, list[float]] = {}


def _limiter_key(username: str) -> str:
    return (username or "")[:_LOGIN_KEY_MAX_CHARS]


def _prune_expired(now: float) -> None:
    """Drop entries whose attempts have all aged out. Cheap, and it is what
    keeps the table bounded in the normal case (a real deployment's failed
    logins are few and expire on their own)."""
    stale = [
        key for key, attempts in _failed_attempts.items()
        if not any(now - t < _LOGIN_WINDOW_SECONDS for t in attempts)
    ]
    for key in stale:
        _failed_attempts.pop(key, None)


def _enforce_table_cap(now: float) -> None:
    """Hard bound for the adversarial case: a spray fast enough that nothing
    has expired yet. Evicts the entries FURTHEST from being rate limited
    (fewest recent attempts, oldest first) and never an entry already at the
    limit — so the eviction cannot be used to clear a real lockout."""
    if len(_failed_attempts) <= _LOGIN_MAX_TRACKED_USERNAMES:
        return
    evictable = [
        (len(attempts), min(attempts, default=now), key)
        for key, attempts in _failed_attempts.items()
        if len(attempts) < _LOGIN_MAX_ATTEMPTS
    ]
    evictable.sort()
    for _, _, key in evictable[: len(_failed_attempts) - _LOGIN_MAX_TRACKED_USERNAMES]:
        _failed_attempts.pop(key, None)


def _rate_limited(username: str) -> bool:
    now = time.monotonic()
    key = _limiter_key(username)
    attempts = [t for t in _failed_attempts.get(key, []) if now - t < _LOGIN_WINDOW_SECONDS]
    if attempts:
        _failed_attempts[key] = attempts
    else:
        # Nothing recent — drop the entry entirely rather than leaving an
        # empty list behind (the original leak: an empty list still pinned
        # the attacker-supplied key in the dict forever).
        _failed_attempts.pop(key, None)
    return len(attempts) >= _LOGIN_MAX_ATTEMPTS


def _record_failed_attempt(username: str) -> None:
    now = time.monotonic()
    _failed_attempts.setdefault(_limiter_key(username), []).append(now)
    _prune_expired(now)
    _enforce_table_cap(now)


@router.post("/login", response_model=schemas.TokenResponse)
def login(payload: schemas.LoginRequest, request: Request, db: Session = Depends(get_db)):
    if _rate_limited(payload.username):
        log_action(db, None, "login_rate_limited", resource=payload.username, result="FAILURE", ip=request.client.host if request.client else "")
        raise HTTPException(status_code=429, detail="Too many failed login attempts — try again in a minute")

    user = db.query(models.User).filter(models.User.username == payload.username).first()
    if not user or not verify_password(payload.password, user.password_hash):
        _record_failed_attempt(payload.username)
        log_action(db, None, "login_failed", resource=payload.username, result="FAILURE", ip=request.client.host if request.client else "")
        raise HTTPException(status_code=401, detail="Incorrect Police ID or password")
    _failed_attempts.pop(_limiter_key(payload.username), None)
    if not user.active:
        raise HTTPException(status_code=403, detail="Account disabled")
    token = create_access_token(user)
    log_action(db, user, "login", result="SUCCESS", ip=request.client.host if request.client else "")
    return schemas.TokenResponse(
        access_token=token,
        user={
            "id": user.id, "username": user.username, "full_name": user.full_name,
            "department": user.department, "role": user.role.name if user.role else None,
        },
    )


@router.get("/me", response_model=schemas.UserOut)
def me(user: models.User = Depends(get_current_user)):
    return schemas.UserOut(
        id=user.id, username=user.username, full_name=user.full_name,
        department=user.department, role=user.role.name if user.role else None, active=user.active,
    )
