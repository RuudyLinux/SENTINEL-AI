from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from .. import models, schemas
from ..db import get_db
from ..security import get_current_user, require_roles, hash_password
from ..audit import log_action

router = APIRouter(prefix="/api", tags=["users"])


@router.get("/roles", response_model=list[schemas.RoleOut])
def list_roles(db: Session = Depends(get_db), user: models.User = Depends(get_current_user)):
    return db.query(models.Role).all()


@router.get("/users", response_model=list[schemas.UserOut])
def list_users(db: Session = Depends(get_db), user: models.User = Depends(require_roles("Administrator"))):
    out = []
    for u in db.query(models.User).all():
        out.append(schemas.UserOut(id=u.id, username=u.username, full_name=u.full_name, department=u.department,
                                    role=u.role.name if u.role else None, active=u.active))
    return out


@router.post("/users", response_model=schemas.UserOut)
def create_user(payload: schemas.UserCreate, db: Session = Depends(get_db), user: models.User = Depends(require_roles("Administrator"))):
    if db.query(models.User).filter(models.User.username == payload.username).first():
        raise HTTPException(status_code=400, detail="Username already exists")
    role = db.query(models.Role).filter(models.Role.name == payload.role_name).first()
    if not role:
        raise HTTPException(status_code=400, detail="Unknown role")
    new_user = models.User(
        username=payload.username, password_hash=hash_password(payload.password),
        full_name=payload.full_name, department=payload.department, role_id=role.id,
    )
    db.add(new_user)
    db.commit()
    db.refresh(new_user)
    log_action(db, user, "create_user", resource=new_user.username)
    return schemas.UserOut(id=new_user.id, username=new_user.username, full_name=new_user.full_name,
                            department=new_user.department, role=role.name, active=new_user.active)


@router.post("/users/{user_id}/disable")
def disable_user(user_id: str, db: Session = Depends(get_db), user: models.User = Depends(require_roles("Administrator"))):
    target = db.query(models.User).filter(models.User.id == user_id).first()
    if not target:
        raise HTTPException(status_code=404, detail="User not found")
    target.active = False
    db.commit()
    log_action(db, user, "disable_user", resource=user_id)
    return {"ok": True}
