"""Shared fixtures.

DB_PATH/UPLOADS_DIR/EVIDENCE_DIR are set before anything under `app` is
imported, so the suite runs on a throwaway SQLite DB and temp dirs, never the
real backend/sentinel.db, uploads/ or evidence_store/.
"""
import os
import tempfile
from pathlib import Path

_tmp_root = Path(tempfile.mkdtemp(prefix="sentinel_test_"))
os.environ.setdefault("DB_PATH", str(_tmp_root / "test.db"))
os.environ.setdefault("UPLOADS_DIR", str(_tmp_root / "uploads"))
os.environ.setdefault("EVIDENCE_DIR", str(_tmp_root / "evidence_store"))
os.environ.setdefault("CAMERA_CATALOG_BASE_URL", "")  # must stay empty unless a test opts in
os.environ.setdefault("DEMO_MODE", "true")
# A dev's backend/.env may have real grid credentials (pydantic-settings reads
# it too). Then every test booting the app would log into the real grid via
# supervisor.discover_and_register() at startup. Force them empty, and
# autoconnect off as a second guard.
os.environ.setdefault("SENTINEL_GRID_EMAIL", "")
os.environ.setdefault("SENTINEL_GRID_PASSWORD", "")
os.environ.setdefault("SENTINEL_GRID_AUTOCONNECT", "false")

import pytest
from fastapi.testclient import TestClient

from app.db import Base, engine, SessionLocal
from app.main import app
from app import models
from app.security import hash_password, create_access_token


@pytest.fixture(scope="session", autouse=True)
def _create_schema():
    Base.metadata.create_all(bind=engine)
    yield


@pytest.fixture
def db_session():
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture
def admin_user(db_session):
    role = db_session.query(models.Role).filter(models.Role.name == "Administrator").first()
    if role is None:
        role = models.Role(name="Administrator", description="test")
        db_session.add(role)
        db_session.flush()
    user = db_session.query(models.User).filter(models.User.username == "test_admin").first()
    if user is None:
        user = models.User(
            username="test_admin", password_hash=hash_password("testpass123"),
            full_name="Test Admin", role_id=role.id,
        )
        db_session.add(user)
        db_session.commit()
        db_session.refresh(user)
    return user


@pytest.fixture
def admin_token(admin_user):
    return create_access_token(admin_user)


# Everything that references cameras, and what references those, children
# first. Deleting cameras alone only worked while SQLite ignored foreign keys;
# with them on it was 143 errors + 5 failures from this fixture alone.
#
# From the ForeignKeys in app/models.py: Evidence -> Incident/Alert/Detection,
# IncidentAlert -> Incident/Alert, Incident -> Alert, Alert ->
# AlertRule/Detection, AlertRule -> Zone, Plate/Track/Detection/Zone/
# SelfHealEvent -> Camera. Vehicles stay, nothing ties them to a camera.
_CAMERA_DEPENDENTS_CHILDREN_FIRST = (
    "Evidence",
    "IncidentNote",
    "IncidentAlert",
    "Incident",
    "Alert",
    "AlertRule",
    "Plate",
    "Track",
    "Detection",
    "Zone",
    "SelfHealEvent",
    "Camera",
)


def _wipe_cameras_and_dependents(session) -> None:
    for model_name in _CAMERA_DEPENDENTS_CHILDREN_FIRST:
        session.query(getattr(models, model_name)).delete(synchronize_session=False)
    session.commit()


def delete_cameras_by_code(session, camera_codes: "list[str]") -> None:
    """Delete the named cameras and everything referencing them, children
    first. With foreign keys on you can't just delete a camera that has
    detections/alerts/incidents."""
    camera_ids = [
        c.id for c in session.query(models.Camera).filter(models.Camera.camera_code.in_(camera_codes)).all()
    ]
    if not camera_ids:
        return
    scoped = {
        "Evidence": models.Evidence.camera_id,
        "Plate": models.Plate.camera_id,
        "Track": models.Track.camera_id,
        "Zone": models.Zone.camera_id,
        "SelfHealEvent": models.SelfHealEvent.camera_id,
    }
    # Incident/Alert/Detection need their own children cleared first.
    incident_ids = [
        i.id for i in session.query(models.Incident).filter(models.Incident.camera_id.in_(camera_ids)).all()
    ]
    alert_ids = [
        a.id for a in session.query(models.Alert).filter(models.Alert.camera_id.in_(camera_ids)).all()
    ]
    if incident_ids or alert_ids:
        session.query(models.Evidence).filter(models.Evidence.incident_id.in_(incident_ids or [""])).delete(synchronize_session=False)
        session.query(models.IncidentNote).filter(models.IncidentNote.incident_id.in_(incident_ids or [""])).delete(synchronize_session=False)
        session.query(models.IncidentAlert).filter(models.IncidentAlert.incident_id.in_(incident_ids or [""])).delete(synchronize_session=False)
        session.query(models.IncidentAlert).filter(models.IncidentAlert.alert_id.in_(alert_ids or [""])).delete(synchronize_session=False)
    for model_name, column in scoped.items():
        session.query(getattr(models, model_name)).filter(column.in_(camera_ids)).delete(synchronize_session=False)
    session.query(models.Incident).filter(models.Incident.camera_id.in_(camera_ids)).delete(synchronize_session=False)
    session.query(models.Alert).filter(models.Alert.camera_id.in_(camera_ids)).delete(synchronize_session=False)
    session.query(models.Detection).filter(models.Detection.camera_id.in_(camera_ids)).delete(synchronize_session=False)
    session.query(models.Camera).filter(models.Camera.id.in_(camera_ids)).delete(synchronize_session=False)
    session.commit()


@pytest.fixture
def client():
    """TestClient(app) runs the real startup, which resumes camera workers
    (real VideoCapture decode + torch inference) for any camera an earlier
    test left in the shared DB (test_demo_scenario's C-014/C-019). Nothing
    owns those tasks, they get killed mid-work at interpreter exit, and that
    crashed the test process with an FFmpeg/torch threading assertion. API
    tests have no business starting workers, so wipe cameras first.

    Dependent rows go too (_CAMERA_DEPENDENTS_CHILDREN_FIRST); deleting only
    cameras left orphans behind, which only worked while SQLite ignored FKs.
    """
    session = SessionLocal()
    try:
        _wipe_cameras_and_dependents(session)
    finally:
        session.close()
    with TestClient(app) as c:
        yield c


@pytest.fixture(autouse=True)
def _no_plate_model_unless_a_test_configures_one(monkeypatch):
    """Production names a trained plate model and a dev box may have the
    weights while CI doesn't. Every test runs on the classical localizer
    unless it sets plate_model_name itself."""
    from app.config import settings
    from app.pipeline import plate_detector
    monkeypatch.setattr(settings, "plate_model_name", "")
    plate_detector.get_plate_model.cache_clear()
    yield
    plate_detector.get_plate_model.cache_clear()


@pytest.fixture(autouse=True)
def _free_ai_slots():
    """AI slots are process-global and tests call _process_frame without the
    stop_worker that frees them, so one test's slot would block the next."""
    from app.pipeline import ai_capacity
    ai_capacity.reset()
    yield
    ai_capacity.reset()
