
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from .. import models, schemas
from ..db import get_db
from .. import runtime_state
from ..config import settings
from ..security import verify_password, create_access_token, get_current_user
from ..audit import log_action

router = APIRouter(prefix="/api/auth", tags=["auth"])

# Login rate limiter keyed by (username, source IP): MAX_ATTEMPTS failures in
# WINDOW_SECONDS lock that key until the window moves on; success clears it.
_LOGIN_MAX_ATTEMPTS = 5
_LOGIN_WINDOW_SECONDS = 60.0

# Keys are attacker-controlled, so both their size and their number are
# bounded. Usernames are truncated to _LOGIN_KEY_MAX_CHARS, and the table is
# capped at _LOGIN_MAX_TRACKED_USERNAMES, evicting only fully aged-out entries,
# so spraying new usernames can't reset a real target's counter.
_LOGIN_KEY_MAX_CHARS = 128
_LOGIN_MAX_TRACKED_USERNAMES = 1024
# Backed by runtime_state.py, so limits are shared across processes and survive
# restarts.
_failed_attempts = runtime_state.build_sliding_window(
    "login_attempts", settings, max_keys=_LOGIN_MAX_TRACKED_USERNAMES,
)


def _limiter_key(username: str, ip: str = "") -> str:
    """Key the counter on (username, source IP), not the username alone.

    Username-only keys let anyone lock a known account (e.g. admin) out from
    everywhere without a credential. Per-address keys give a distributed
    attacker more attempts, which bcrypt, audited failures and proxy-level
    rate limiting mitigate.
    """
    return f"{(username or '')[:_LOGIN_KEY_MAX_CHARS]}|{(ip or '')[:64]}"


def _rate_limited(username: str, ip: str = "") -> bool:
    """Read-only check, so polling it can't lock the account out. Fully aged-out
    entries are still dropped."""
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
        # Audit it: a correct password on a disabled account is a revoked
        # credential still in use.
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
