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
    # bcrypt truncates at 72 bytes. done by hand since passlib's backend
    # detection breaks on bcrypt>=4.1
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
    """get_current_user's JWT check without the header machinery, for the
    WebSocket handshake where the token comes as a query param (main.py /ws).
    Returns None instead of raising; the caller decides what that means."""
    if not token:
        return None
    try:
        payload = jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algorithm])
        user_id = payload.get("sub")
        if user_id is None:
            return None
    except JWTError:
        return None
    # Resource tokens use the same secret and `sub`, so without this one
    # passed as a full session token. They sit in URLs (browser history, proxy
    # logs) and stream tokens last an hour: holding one meant the whole API as
    # that user. Only session tokens have no `scope`.
    if "scope" in payload:
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


# Roles that act on alerts, incidents and plate reads. Not Auditor: an
# auditor who can dismiss the alerts or close the incidents they audit isn't
# an auditor.
OPERATIONAL_ROLES = ("Administrator", "Control Room Operator", "Investigator", "Supervisor")
require_operational_role = require_roles(*OPERATIONAL_ROLES)


# Resource tokens: short-lived and scoped to one resource, for URLs a browser
# loads via plain <img src>/<a href> and can't send a bearer header with. The
# client gets one from a normal authenticated request and adds ?token=. Same
# JWT secret, no server-side state.

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


def resource_token_expiry(token: str) -> "datetime | None":
    """When this resource token expires, or None if it can't be read. exp is
    checked when a token is presented, but a long response like an MJPEG
    stream is authorized once, so the caller needs the deadline to cut it off.
    """
    try:
        payload = jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algorithm])
    except JWTError:
        return None
    exp = payload.get("exp")
    return datetime.utcfromtimestamp(exp) if exp else None
