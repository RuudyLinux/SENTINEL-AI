"""Seeds roles always, and (only in DEMO_MODE) demo user accounts + one demo
watchlist plate on first boot.

No fake cameras/detections/alerts are seeded — those only appear once a real
camera source is added and the pipeline actually runs (see README "Scope & Honesty").
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

    # Demo accounts + demo watchlist entry only in DEMO_MODE (P0-G). Roles
    # above are always seeded — RBAC needs them regardless, and a real
    # deployment still needs to attach a manually-created admin to one.
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


# --- Demo camera fixtures (Phase 6) --------------------------------------
# The two cameras the primary judge-demo scenario runs against. Both point at
# a small tracked-in-git demo clip (app/demo_assets/car-detection.mp4) — real
# YOLO/ByteTrack/EasyOCR processing runs on both; only the specific ANPR
# "read" of the demo watchlist plate is injected deterministically (see
# pipeline/demo_scenario.py), never the detection/tracking itself.
#
# Real bug this fixes: this used to point at "uploads/car-detection.mp4" — a
# file that only ever existed on one developer's machine. `backend/uploads/`
# is gitignored (real operator-uploaded evidence never belongs in version
# control), so a fresh checkout, a Docker build, or any CI runner had NO such
# file: the documented judge-demo flow (README's "Cameras -> Add Camera...
# or Investigate -> Generate Evidence Package") silently could not produce a
# real decoded frame anywhere except that one machine. Caught by this
# project's own CI actually running for the first time. The demo clip now
# lives under app/, which IS tracked, specifically so it ships with the code.
DEMO_CAMERAS = [
    {"camera_code": "C-014", "name": "Ahmedabad Ring Road", "location": "Ahmedabad",
     "lat": 23.03, "lng": 72.58, "source_type": "video_file", "source_uri": "app/demo_assets/car-detection.mp4"},
    {"camera_code": "C-019", "name": "Naroda Junction", "location": "Naroda",
     "lat": 23.07, "lng": 72.65, "source_type": "video_file", "source_uri": "app/demo_assets/car-detection.mp4"},
]
DEMO_PLATE = "GJ05AB1234"


def reset_demo_data(db: Session) -> dict:
    """Returns the app to a clean, repeatable demo state: wipes transactional
    data (detections/plates/vehicles/tracks/alerts/incidents/evidence — NOT
    users, roles, or the audit trail itself, which should keep recording),
    and ensures the two demo cameras + the demo watchlist entry exist.

    DEMO_MODE only — the router enforces this; this function additionally
    refuses to run otherwise as a second guard against ever wiping real
    operational data by accident.
    """
    if not settings.demo_mode:
        raise RuntimeError("reset_demo_data called outside DEMO_MODE — refusing")

    # BUG-D fix (final deep-debug pass, 2026-09-11): `models.IncidentAlert` was
    # MISSING from this list, and it holds foreign keys to BOTH incidents and
    # alerts — so `DELETE FROM incidents` here violated referential integrity.
    # That silently "worked" only because SQLite ignores foreign keys unless
    # `PRAGMA foreign_keys=ON` (now set — see app/db.py); on PostgreSQL, which
    # has always enforced them, this raised a ForeignKeyViolation, meaning
    # `POST /api/system/demo/reset` — the flagship judge-demo reset path — was
    # broken on the production datastore and no SQLite-run test could see it.
    #
    # Order matters and is children-first: IncidentAlert before Incident/Alert,
    # Alert before Detection (Alert.detection_id), everything referencing a
    # vehicle before Vehicle itself.
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
