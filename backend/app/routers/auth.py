from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from .. import models, schemas
from ..db import get_db
from ..security import verify_password, create_access_token, get_current_user
from ..audit import log_action

router = APIRouter(prefix="/api/auth", tags=["auth"])


@router.post("/login", response_model=schemas.TokenResponse)
def login(payload: schemas.LoginRequest, request: Request, db: Session = Depends(get_db)):
    user = db.query(models.User).filter(models.User.username == payload.username).first()
    if not user or not verify_password(payload.password, user.password_hash):
        log_action(db, None, "login_failed", resource=payload.username, result="FAILURE", ip=request.client.host if request.client else "")
        raise HTTPException(status_code=401, detail="Incorrect Police ID or password")
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
