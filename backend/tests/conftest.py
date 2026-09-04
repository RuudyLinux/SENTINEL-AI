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
    guarantees there's nothing for the startup resume-loop to find."""
    session = SessionLocal()
    try:
        session.query(models.Camera).delete()
        session.commit()
    finally:
        session.close()
    with TestClient(app) as c:
        yield c
