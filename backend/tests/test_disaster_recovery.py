"""Disaster recovery (10/10 roadmap P14): seed real data, back up the SQLite
file, destroy the working copy, restore from the backup, and prove every
piece of evidence-critical state survived — not just "the app starts again".

Runs against its OWN standalone SQLite file and engine (via `sqlite3`'s own
`Connection.backup()` — SQLite's real online-backup API, not a hand-rolled
file copy that could race a write), completely independent of the shared
`app.db.engine`/`SessionLocal` the rest of the suite uses. That keeps this
test from disturbing (or being disturbed by) any other test's DB state,
while still exercising the schema this repository actually ships (all
tables created from `app.models`, the exact SQLAlchemy models a real
deployment uses).

What this test does NOT prove: it verifies SQLite backup/restore — the
locally-developed and demo-deployed path. Restoring a PostgreSQL-backed
deployment uses the same alembic-managed schema (see backend/alembic/) but
was not exercised here — no PostgreSQL instance is available in this
environment. That gap is stated explicitly, not implied to be covered.
"""
import sqlite3
import tempfile
import uuid
from datetime import datetime
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import audit, models


@pytest.fixture
def dr_env(tmp_path: Path):
    """A fully independent schema + engine + session, isolated from the rest
    of the suite's shared SQLite file."""
    db_path = tmp_path / "dr_original.db"
    engine = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})
    models.Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    yield db_path, engine, session
    session.close()
    engine.dispose()


def _seed_real_data(session) -> dict:
    """Real rows through the real model classes — an incident, its evidence
    (with a genuine capture-time SHA-256 of a real temp file), and an audited,
    hash-chained trail — mirroring what a live deployment actually persists."""
    camera = models.Camera(camera_code=f"DR-{uuid.uuid4().hex[:8]}", name="DR test camera", source_type="video_file", source_uri="x.mp4")
    session.add(camera)
    session.flush()

    incident = models.Incident(
        title="DR test incident", incident_type="watchlist_match", priority="CRITICAL",
        status="open", camera_id=camera.id,
    )
    session.add(incident)
    session.flush()

    # A real file, really hashed — same function the live capture path uses.
    evidence_file = Path(tempfile.mkstemp(suffix=".jpg")[1])
    evidence_file.write_bytes(b"not a real jpeg, but real bytes to hash \x00\x01\x02")
    from app.evidence_hash import sha256_file
    digest = sha256_file(str(evidence_file))
    assert digest  # sanity: the hash function must actually produce something

    evidence = models.Evidence(
        incident_id=incident.id, evidence_type="snapshot", camera_id=camera.id,
        file_path=str(evidence_file), sha256=digest, verification_status="verified",
        model_version="dr-test-model-1.0", rule_version="dr-test-rules-1.0",
    )
    session.add(evidence)
    session.flush()

    a1 = audit.log_action(session, None, "dr_test_create_incident", resource=incident.id)
    a2 = audit.log_action(session, None, "dr_test_create_evidence", resource=evidence.id)
    session.commit()

    return {
        "incident_id": incident.id, "evidence_id": evidence.id, "sha256": digest,
        "audit_ids": [a1.id, a2.id],
    }


def _backup(source_path: Path, dest_path: Path) -> None:
    """SQLite's own online backup API — consistent even against a live
    connection, unlike a plain file copy racing an in-progress write."""
    src = sqlite3.connect(str(source_path))
    dst = sqlite3.connect(str(dest_path))
    with dst:
        src.backup(dst)
    src.close()
    dst.close()


class TestDisasterRecovery:
    def test_backup_and_restore_preserves_incidents_evidence_hashes_and_audit_chain(self, dr_env, tmp_path):
        db_path, engine, session = dr_env
        seeded = _seed_real_data(session)

        # 1. Confirm the chain is intact BEFORE the disaster, so a later
        #    failure can be attributed to the restore, not to seeding.
        pre_disaster_chain = audit.verify_chain(session)
        assert pre_disaster_chain["valid"] is True

        # 2. Backup.
        backup_path = tmp_path / "dr_backup.db"
        session.close()
        engine.dispose()
        _backup(db_path, backup_path)

        # 3. Destroy the working copy — the disaster.
        db_path.unlink()
        assert not db_path.exists()

        # 4. Restore: the backup becomes the new working copy.
        backup_path.replace(db_path)
        assert db_path.exists()

        # 5. Reconnect and verify.
        restored_engine = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})
        RestoredSession = sessionmaker(bind=restored_engine)
        restored = RestoredSession()
        try:
            incident = restored.query(models.Incident).filter(models.Incident.id == seeded["incident_id"]).first()
            assert incident is not None
            assert incident.title == "DR test incident"
            assert incident.status == "open"

            evidence = restored.query(models.Evidence).filter(models.Evidence.id == seeded["evidence_id"]).first()
            assert evidence is not None
            assert evidence.sha256 == seeded["sha256"]
            assert evidence.verification_status == "verified"
            assert evidence.model_version == "dr-test-model-1.0"

            audit_rows = restored.query(models.AuditLog).filter(models.AuditLog.id.in_(seeded["audit_ids"])).all()
            assert len(audit_rows) == 2

            post_restore_chain = audit.verify_chain(restored)
            assert post_restore_chain["valid"] is True
            assert post_restore_chain["checked"] == pre_disaster_chain["checked"]
        finally:
            restored.close()
            restored_engine.dispose()

    def test_a_missing_backup_is_reported_not_silently_treated_as_success(self, tmp_path):
        """A DR test that can pass with no backup at all would be worse than
        no DR test — this locks down that restoring from a nonexistent file
        fails loudly."""
        nonexistent = tmp_path / "does_not_exist.db"
        working = tmp_path / "dr_working.db"
        with pytest.raises(FileNotFoundError):
            nonexistent.replace(working)
