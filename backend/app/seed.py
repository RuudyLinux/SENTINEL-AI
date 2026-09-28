"""Seed roles always, and in DEMO_MODE the demo accounts and one demo
watchlist plate on first boot.

No fake cameras, detections or alerts; those only show up once a real camera
is added and the pipeline runs (README "Scope & Honesty").
"""
from datetime import datetime

from sqlalchemy.orm import Session
from . import models
from .config import settings
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

    # demo accounts and watchlist entry only in DEMO_MODE. roles always,
    # RBAC needs them and a real deploy still attaches an admin to one
    if settings.demo_mode:
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


# The two cameras the demo scenario uses. Both play a small demo clip kept in
# git (app/demo_assets/car-detection.mp4) through real YOLO/ByteTrack/EasyOCR;
# only the read of the demo watchlist plate is injected
# (pipeline/demo_scenario.py).
#
# It's under app/ because backend/uploads/ is gitignored: pointing at
# uploads/car-detection.mp4 only worked on one dev machine, and a fresh
# checkout, Docker build or CI had no file to decode. CI caught it.
DEMO_CAMERAS = [
    {"camera_code": "C-014", "name": "Ahmedabad Ring Road", "location": "Ahmedabad",
     "lat": 23.03, "lng": 72.58, "source_type": "video_file", "source_uri": "app/demo_assets/car-detection.mp4"},
    {"camera_code": "C-019", "name": "Naroda Junction", "location": "Naroda",
     "lat": 23.07, "lng": 72.65, "source_type": "video_file", "source_uri": "app/demo_assets/car-detection.mp4"},
]
DEMO_PLATE = "GJ05AB1234"


def reset_demo_data(db: Session) -> dict:
    """Back to a clean, repeatable demo state: wipe transactional data
    (detections, plates, vehicles, tracks, alerts, incidents, evidence; not
    users, roles or the audit trail) and make sure the demo cameras and
    watchlist entry exist.

    DEMO_MODE only. The router checks, and so does this, so real data can't
    be wiped by accident.
    """
    if not settings.demo_mode:
        raise RuntimeError("reset_demo_data called outside DEMO_MODE — refusing")

    # Children first: IncidentAlert before Incident/Alert, Alert before
    # Detection (Alert.detection_id), anything pointing at a vehicle before
    # Vehicle. IncidentAlert was missing once; SQLite without FK enforcement
    # didn't mind, PostgreSQL raised and demo reset was broken in production.
    for model in (models.Evidence, models.IncidentNote, models.IncidentAlert,
                  models.Incident, models.Alert,
                  models.Plate, models.Track, models.Detection, models.Vehicle):
        db.query(model).delete()

    cameras_by_code = {c.camera_code: c for c in db.query(models.Camera).all()}
    for spec in DEMO_CAMERAS:
        camera = cameras_by_code.get(spec["camera_code"])
        if camera is None:
            camera = models.Camera(**spec)
            db.add(camera)
        else:
            camera.status = "offline"
            camera.error_count = 0
            camera.last_frame_at = None

    if not db.query(models.WatchlistEntry).filter(models.WatchlistEntry.identifier == DEMO_PLATE).first():
        db.add(models.WatchlistEntry(
            entity_type="plate", identifier=DEMO_PLATE,
            reason="Demo watchlist entry — vehicle of interest (doc §60 flagship scenario)",
            priority="CRITICAL",
        ))

    db.commit()
    return {
        "reset_at": datetime.utcnow().isoformat(),
        "cameras": [c["camera_code"] for c in DEMO_CAMERAS],
        "watchlist_plate": DEMO_PLATE,
    }
