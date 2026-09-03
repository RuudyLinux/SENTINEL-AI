"""Seeds roles, an admin/operator user, and one demo watchlist plate on first boot.

No fake cameras/detections/alerts are seeded — those only appear once a real
camera source is added and the pipeline actually runs (see README "Scope & Honesty").
"""
from sqlalchemy.orm import Session
from . import models
from .security import hash_password

ROLE_DEFS = [
    ("Administrator", "Full system administration."),
    ("Control Room Operator", "Live monitoring, alerts and operational actions."),
    ("Investigator", "Search, cases and evidence."),
    ("Supervisor", "Review, reporting and approval functions."),
    ("Auditor", "Audit-log and compliance visibility."),
]

USER_DEFS = [
    # username, password, full_name, department, role
    ("admin", "sentinel123", "System Administrator", "HQ", "Administrator"),
    ("operator1", "sentinel123", "Control Room Operator", "Ahmedabad", "Control Room Operator"),
    ("investigator1", "sentinel123", "Case Investigator", "Ahmedabad", "Investigator"),
    ("auditor1", "sentinel123", "Compliance Auditor", "HQ", "Auditor"),
]


def run_seed(db: Session) -> None:
    roles_by_name: dict[str, models.Role] = {}
    for name, desc in ROLE_DEFS:
        role = db.query(models.Role).filter(models.Role.name == name).first()
        if not role:
            role = models.Role(name=name, description=desc)
            db.add(role)
            db.flush()
        roles_by_name[name] = role

    for username, password, full_name, department, role_name in USER_DEFS:
        existing = db.query(models.User).filter(models.User.username == username).first()
        if not existing:
            db.add(models.User(
                username=username,
                password_hash=hash_password(password),
                full_name=full_name,
                department=department,
                role_id=roles_by_name[role_name].id,
            ))

    if not db.query(models.WatchlistEntry).first():
        db.add(models.WatchlistEntry(
            entity_type="plate",
            identifier="GJ05AB1234",
            reason="Demo watchlist entry — vehicle of interest (doc §60 flagship scenario)",
            priority="CRITICAL",
        ))

    db.commit()
