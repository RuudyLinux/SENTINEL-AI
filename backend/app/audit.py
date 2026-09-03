from sqlalchemy.orm import Session
from . import models


def log_action(db: Session, user: models.User | None, action: str, resource: str = "", result: str = "SUCCESS", ip: str = ""):
    entry = models.AuditLog(
        user_id=user.id if user else None,
        username=user.username if user else "anonymous",
        action=action,
        resource=resource,
        result=result,
        ip=ip,
    )
    db.add(entry)
    db.commit()
