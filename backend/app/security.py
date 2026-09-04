from datetime import datetime, timedelta
from typing import Optional

import bcrypt
from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from jose import jwt, JWTError
from sqlalchemy.orm import Session

from .config import settings
from .db import get_db
from . import models

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/auth/login", auto_error=False)


def hash_password(password: str) -> str:
    # bcrypt truncates at 72 bytes; enforced explicitly instead of via passlib
    # (passlib's bcrypt backend-detection is broken on bcrypt>=4.1 as of this build).
    return bcrypt.hashpw(password.encode("utf-8")[:72], bcrypt.gensalt()).decode("utf-8")


def verify_password(plain: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(plain.encode("utf-8")[:72], hashed.encode("utf-8"))
    except ValueError:
        return False


def create_access_token(user: models.User) -> str:
    expire = datetime.utcnow() + timedelta(minutes=settings.access_token_minutes)
    role_name = user.role.name if user.role else ""
    payload = {"sub": user.id, "username": user.username, "role": role_name, "exp": expire}
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def get_user_from_token(token: Optional[str], db: Session) -> Optional[models.User]:
    """Same JWT validation as get_current_user, usable outside the HTTP
    Authorization-header/OAuth2PasswordBearer machinery — for the WebSocket
    handshake (browsers can't attach a header there; the token travels as a
    query parameter instead, see main.py's /ws). Returns None rather than
    raising; the caller decides what "no valid user" means for its transport."""
    if not token:
        return None
    try:
        payload = jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algorithm])
        user_id = payload.get("sub")
        if user_id is None:
            return None
    except JWTError:
        return None
    user = db.query(models.User).filter(models.User.id == user_id).first()
    if user is None or not user.active:
        return None
    return user


def get_current_user(
    token: Optional[str] = Depends(oauth2_scheme), db: Session = Depends(get_db)
) -> models.User:
    credentials_exc = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )
    user = get_user_from_token(token, db)
    if user is None:
        raise credentials_exc
    return user


def require_roles(*allowed_roles: str):
    def dependency(user: models.User = Depends(get_current_user)) -> models.User:
        role_name = user.role.name if user.role else ""
        if allowed_roles and role_name not in allowed_roles:
            raise HTTPException(status_code=403, detail=f"Role '{role_name}' not permitted for this action")
        return user
    return dependency


# --- Resource tokens (P0-E) ---------------------------------------------
# Short-lived, scoped-to-one-resource signed tokens for the handful of
# endpoints browsers hit via plain <img src>/<a href> navigation, which
# can't attach an Authorization: Bearer header. The client fetches a token
# via a normal authenticated (RBAC-checked) request first, then appends it
# as `?token=` on the actual file/stream URL. Reuses the same JWT secret —
# no new dependency, no server-side session state.

def create_resource_token(resource: str, resource_id: str, user: models.User, ttl_seconds: int) -> str:
    expire = datetime.utcnow() + timedelta(seconds=ttl_seconds)
    payload = {"sub": user.id, "scope": f"{resource}:{resource_id}", "exp": expire}
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def get_user_from_resource_token(resource: str, resource_id: str, token: str, db: Session) -> models.User:
    credentials_exc = HTTPException(status_code=401, detail="Invalid or expired resource token")
    try:
        payload = jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algorithm])
    except JWTError:
        raise credentials_exc
    if payload.get("scope") != f"{resource}:{resource_id}":
        raise credentials_exc
    user_id = payload.get("sub")
    user = db.query(models.User).filter(models.User.id == user_id).first()
    if user is None or not user.active:
        raise credentials_exc
    return user
