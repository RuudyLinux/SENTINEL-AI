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
_failed_attempts: dict[str, list[float]] = {}


def _rate_limited(username: str) -> bool:
    now = time.monotonic()
    attempts = [t for t in _failed_attempts.get(username, []) if now - t < _LOGIN_WINDOW_SECONDS]
    _failed_attempts[username] = attempts
    return len(attempts) >= _LOGIN_MAX_ATTEMPTS


def _record_failed_attempt(username: str) -> None:
    _failed_attempts.setdefault(username, []).append(time.monotonic())


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
    _failed_attempts.pop(payload.username, None)
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
