
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from .. import models, schemas
from ..db import get_db
from .. import runtime_state
from ..config import settings
from ..security import verify_password, create_access_token, get_current_user
from ..audit import log_action

router = APIRouter(prefix="/api/auth", tags=["auth"])

# Login rate limiter, a first layer against brute force, keyed by (username,
# source IP) (see _limiter_key). MAX_ATTEMPTS failures inside WINDOW_SECONDS
# lock that key out until the window moves on; a success clears it.
_LOGIN_MAX_ATTEMPTS = 5
_LOGIN_WINDOW_SECONDS = 60.0

# The keys are attacker-controlled, and every failed login used to add one for
# good: 500 new usernames = 500 keys, one 1,000,000-char username = a 1MB key.
# Unauthenticated, unbounded memory growth.
#
# Both are bounded now:
#   - key size: username truncated to _LOGIN_KEY_MAX_CHARS. only names sharing
#     a 128-char prefix collide, and that only makes limiting stricter
#   - key count: capped at _LOGIN_MAX_TRACKED_USERNAMES, evicting only keys
#     whose attempts have all aged out. an entry at the limit is never evicted,
#     otherwise spraying new usernames would reset a real target's counter
#     (tests/test_login_ratelimit_hardening.py)
_LOGIN_KEY_MAX_CHARS = 128
_LOGIN_MAX_TRACKED_USERNAMES = 1024
# The store is app/runtime_state.py now, same eviction rules. Not monotonic
# time anymore: per-process readings meant a second API process counted the
# same failures separately and neither hit the limit, and a restart cleared
# every lockout.
_failed_attempts = runtime_state.build_sliding_window(
    "login_attempts", settings, max_keys=_LOGIN_MAX_TRACKED_USERNAMES,
)


def _limiter_key(username: str, ip: str = "") -> str:
    """Key the counter on (username, source IP), not username alone.

    Username only made lockout trivial: five wrong passwords against "admin"
    (it's in the README) locked the real admin out from everywhere. In a
    control room that's an availability attack needing no credential, and an
    operator locked out mid-incident is worse than the brute force it stops.

    Trade-off: an attacker with many addresses gets _LOGIN_MAX_ATTEMPTS per
    address instead of five total. That's the price of not being remotely
    lockable, and this isn't the only defence: bcrypt, every failure
    audited, and a real deploy rate-limits by address at the proxy.
    """
    return f"{(username or '')[:_LOGIN_KEY_MAX_CHARS]}|{(ip or '')[:64]}"


def _rate_limited(username: str, ip: str = "") -> bool:
    """Read-only, or a client polling login would lock out the account it's
    asking about. count() still drops a fully aged-out entry so an empty list
    can't pin an attacker's key forever."""
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
        # audit it: right password on a disabled account means a revoked
        # operator or a credential still in use after the account closed.
        # this used to 403 with no trace
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
