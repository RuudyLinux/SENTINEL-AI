"""Shared pytest fixtures for the Phase 3 regression suite.

IMPORTANT: the DB_PATH/UPLOADS_DIR/EVIDENCE_DIR env vars are set BEFORE
anything under `app` is imported, so every test in this suite runs against
a throwaway temp SQLite DB and throwaway storage dirs — never the real
`backend/sentinel.db` / `uploads/` / `evidence_store/` a developer is using.
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
# Real credentials for the Sentinel Camera Grid may exist in a developer's
# real backend/.env (pydantic-settings reads it too, not just os.environ) —
# without this override, every test that boots the real FastAPI app (the
# `client` fixture) would trigger a REAL network call to the real external
# grid via supervisor.discover_and_register() at startup: real login latency
# per test, and real repeated hits against the live grid. Force both empty
# so credentials are "not configured" for the whole suite regardless of what
# a developer's .env holds; AUTOCONNECT is also forced off as a second,
# independent guard against any accidental real connection during a test.
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


# Every table that references `cameras`, plus everything that references
# THOSE, ordered children-first. Deleting cameras alone (what this fixture
# used to do) is only valid while SQLite has foreign keys switched off; the
# moment `PRAGMA foreign_keys=ON` matches the PostgreSQL schema's real
# behavior, an unordered camera wipe fails outright — measured: 143 errors
# + 5 failures across the suite, all from this one fixture.
#
# Derived from the actual ForeignKey declarations in app/models.py, not
# guessed: Evidence -> Incident/Alert/Detection, IncidentAlert ->
# Incident/Alert, Incident -> Alert, Alert -> AlertRule/Detection,
# AlertRule -> Zone, Plate/Track/Detection/Zone/SelfHealEvent -> Camera.
# Vehicles are deliberately NOT wiped: nothing links them to a camera, so
# removing them would discard more shared state than this fixture needs to.
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
    """Remove specific cameras AND everything referencing them, children-first.

    With `PRAGMA foreign_keys=ON` (app/db.py), deleting a camera that still
    has detections/alerts/incidents simply fails — so a test that needs a
    camera code to be absent cannot just delete the camera row. Shares the
    same ordering as the full wipe above, scoped to the named codes.
    """
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
    """Phase 6 finding: TestClient(app) runs the real FastAPI startup event,
    which resumes real camera workers (real cv2.VideoCapture decode + real
    torch inference, as background asyncio tasks) for any Camera row left
    in the shared test DB by an earlier test (e.g. test_demo_scenario.py's
    C-014/C-019). Those tasks have no test-level owner to await or cancel,
    so they keep running past the test and get torn down mid-operation at
    interpreter exit — which reproducibly crashed the whole test process
    with a native FFmpeg/torch threading assertion. API-route tests have no
    business starting real camera AI workers at all, so this fixture
    guarantees there's nothing for the startup resume-loop to find.

    Wipes the camera's dependent rows too (see
    `_CAMERA_DEPENDENTS_CHILDREN_FIRST`): deleting only cameras left every
    detection/alert/incident/evidence row behind pointing at a camera_id
    that no longer existed — harmless only because SQLite was ignoring
    foreign keys, and the thing blocking this project from enforcing them.
    """
    session = SessionLocal()
    try:
        _wipe_cameras_and_dependents(session)
    finally:
        session.close()
    with TestClient(app) as c:
        yield c
