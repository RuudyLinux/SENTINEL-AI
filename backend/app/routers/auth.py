
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from .. import models, schemas
from ..db import get_db
from .. import runtime_state
from ..config import settings
from ..security import verify_password, create_access_token, get_current_user
from ..audit import log_action

router = APIRouter(prefix="/api/auth", tags=["auth"])

# In-memory login rate limiter (Phase 11 security baseline) — a first layer
# against credential brute-forcing, keyed by (username, source IP): see
# `_limiter_key` for why username alone made targeted account LOCKOUT a
# credential-free availability attack, and what the pair costs. Single-process,
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
# Both bounds above now live in the shared store (app/runtime_state.py), which
# carries this module's eviction policy verbatim — including the rule that an
# entry already at the limit is never evicted. The move off `time.monotonic()`
# matters beyond tidiness: a monotonic reading is per-process, so with a second
# API process the same five failures are counted twice and neither side ever
# reaches the limit, and a restart cleared every lockout outright.
_failed_attempts = runtime_state.build_sliding_window(
    "login_attempts", settings, max_keys=_LOGIN_MAX_TRACKED_USERNAMES,
)


def _limiter_key(username: str, ip: str = "") -> str:
    """Scope the counter to (username, source IP), not username alone.

    Username-only keying made account LOCKOUT trivially reachable: five wrong
    passwords against a known account — "admin" is documented in this repo's
    own README — denied that operator login for the whole window, from
    anywhere. For a control room that is an availability attack requiring no
    credential at all, and it is worse than the brute-force it defends
    against, because a control-room operator locked out mid-incident is a
    real operational failure.

    Scoping by pair means an attacker hammering `admin` from their own
    address cannot lock out the real admin logging in from theirs.

    The trade-off, stated rather than hidden: an attacker controlling many
    source addresses now gets `_LOGIN_MAX_ATTEMPTS` tries per address instead
    of five in total. That is the accepted cost of not being remotely
    lockout-able, and this limiter was never the only defence — passwords are
    bcrypt-hashed, every failure is audited, and a real deployment fronts this
    with a reverse proxy that can rate-limit by address at the edge.
    """
    return f"{(username or '')[:_LOGIN_KEY_MAX_CHARS]}|{(ip or '')[:64]}"


def _rate_limited(username: str, ip: str = "") -> bool:
    """Read-only: counting must not itself count as an attempt, or a client
    polling the login endpoint would lock out the account it is asking about.
    `count()` still drops an entry whose attempts have all aged out, which is
    what kept an empty list from pinning an attacker-supplied key forever."""
    attempts = _failed_attempts.count(_limiter_key(username, ip), _LOGIN_WINDOW_SECONDS)
    return attempts >= _LOGIN_MAX_ATTEMPTS


def _record_failed_attempt(username: str, ip: str = "") -> None:
    _failed_attempts.record(
        _limiter_key(username, ip), _LOGIN_WINDOW_SECONDS, limit=_LOGIN_MAX_ATTEMPTS,
    )


@router.post("/login", response_model=schemas.TokenResponse)
def login(payload: schemas.LoginRequest, request: Request, db: Session = Depends(get_db)):
    client_ip = request.client.host if request.client else ""
    if _rate_limited(payload.username, client_ip):
        log_action(db, None, "login_rate_limited", resource=payload.username, result="FAILURE", ip=client_ip)
        raise HTTPException(status_code=429, detail="Too many failed login attempts — try again in a minute")

    user = db.query(models.User).filter(models.User.username == payload.username).first()
    if not user or not verify_password(payload.password, user.password_hash):
        _record_failed_attempt(payload.username, client_ip)
        log_action(db, None, "login_failed", resource=payload.username, result="FAILURE", ip=client_ip)
        raise HTTPException(status_code=401, detail="Incorrect Police ID or password")
    _failed_attempts.forget(_limiter_key(payload.username, client_ip))
    if not user.active:
        # Audited: correct credentials against a DISABLED account is exactly
        # the event worth seeing — a revoked operator still holding a working
        # password, or a credential in use after an account was closed. This
        # branch previously returned 403 and recorded nothing, so the attempt
        # left no trace anywhere.
        log_action(db, None, "login_disabled_account", resource=payload.username, result="FAILURE", ip=client_ip)
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
