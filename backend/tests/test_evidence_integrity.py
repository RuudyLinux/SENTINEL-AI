"""Evidence integrity: the hash is a capture-time baseline and verify compares.

verify used to hash the file at verification time, store that and say
"verified". With nothing captured to compare against, a file changed after
capture was reported as verified.
"""
import hashlib

import pytest

from app import models
from app.evidence_hash import sha256_file
from app.db import SessionLocal


@pytest.fixture
def auth(admin_token):
    return {"Authorization": f"Bearer {admin_token}"}


@pytest.fixture
def evidence_file(tmp_path):
    path = tmp_path / "snapshot.jpg"
    path.write_bytes(b"original captured frame bytes")
    return path


def _evidence(file_path, sha256: str | None):
    db = SessionLocal()
    try:
        row = models.Evidence(
            evidence_type="snapshot", file_path=str(file_path) if file_path else None,
            sha256=sha256, verification_status="unverified",
        )
        db.add(row)
        db.commit()
        db.refresh(row)
        return row.id
    finally:
        db.close()


class TestHashHelper:
    def test_matches_a_plain_sha256_of_the_file(self, evidence_file):
        expected = hashlib.sha256(evidence_file.read_bytes()).hexdigest()
        assert sha256_file(str(evidence_file)) == expected

    def test_chunked_reading_gives_the_same_digest_for_a_large_file(self, tmp_path):
        """Chunked hashing of an MP4 must match a whole-file hash, or every
        comparison is a false mismatch."""
        big = tmp_path / "clip.mp4"
        big.write_bytes(b"x" * (3 * 1024 * 1024 + 17))
        assert sha256_file(str(big)) == hashlib.sha256(big.read_bytes()).hexdigest()

    def test_returns_empty_rather_than_raising_on_a_missing_file(self):
        """A captured snapshot is still recorded if hashing fails."""
        assert sha256_file("/no/such/file.jpg") == ""
        assert sha256_file(None) == ""
        assert sha256_file("") == ""


class TestVerification:
    def test_an_unmodified_file_verifies(self, client, auth, evidence_file):
        eid = _evidence(evidence_file, sha256_file(str(evidence_file)))

        body = client.post(f"/api/evidence/{eid}/verify", headers=auth).json()

        assert body["status"] == "verified"
        assert body["ok"] is True

    def test_a_modified_file_is_reported_as_tampered(self, client, auth, evidence_file):
        """What the old code got wrong: rehash the altered file, call it verified."""
        eid = _evidence(evidence_file, sha256_file(str(evidence_file)))
        evidence_file.write_bytes(b"substituted frame bytes")

        body = client.post(f"/api/evidence/{eid}/verify", headers=auth).json()

        assert body["status"] == "tampered"
        assert body["ok"] is False

    def test_the_capture_time_digest_is_never_overwritten_by_a_tamper_check(self, client, auth, evidence_file):
        """The original digest is the record; overwriting it would erase the
        proof of tampering."""
        baseline = sha256_file(str(evidence_file))
        eid = _evidence(evidence_file, baseline)
        evidence_file.write_bytes(b"substituted frame bytes")

        body = client.post(f"/api/evidence/{eid}/verify", headers=auth).json()

        assert body["sha256"] == baseline, "the capture-time digest must survive"
        assert body["computed_sha256"] != baseline
        db = SessionLocal()
        try:
            assert db.query(models.Evidence).filter(models.Evidence.id == eid).one().sha256 == baseline
        finally:
            db.close()

    def test_a_record_with_no_baseline_does_not_claim_to_be_verified(self, client, auth, evidence_file):
        """Evidence from before capture-time hashing can't be confirmed
        unaltered and doesn't pretend to be."""
        eid = _evidence(evidence_file, None)

        body = client.post(f"/api/evidence/{eid}/verify", headers=auth).json()

        assert body["status"] == "no_baseline"
        assert body["ok"] is False
        # A baseline is established so future checks are meaningful.
        assert body["sha256"] == sha256_file(str(evidence_file))

    def test_a_missing_file_is_unverifiable_not_verified(self, client, auth, tmp_path):
        eid = _evidence(tmp_path / "deleted.jpg", "a" * 64)

        body = client.post(f"/api/evidence/{eid}/verify", headers=auth).json()

        assert body["status"] == "unverifiable"
        assert body["ok"] is False

    def test_a_failed_verification_is_audited_as_a_failure(self, client, auth, evidence_file):
        """A tamper result goes in the audit trail as what it is."""
        eid = _evidence(evidence_file, sha256_file(str(evidence_file)))
        evidence_file.write_bytes(b"substituted")
        client.post(f"/api/evidence/{eid}/verify", headers=auth)

        db = SessionLocal()
        try:
            entry = (
                db.query(models.AuditLog)
                .filter(models.AuditLog.action == "verify_evidence", models.AuditLog.resource == eid)
                .order_by(models.AuditLog.timestamp.desc())
                .first()
            )
            assert entry is not None and entry.result == "FAILURE"
        finally:
            db.close()

    def test_verification_requires_authentication(self, client, evidence_file):
        eid = _evidence(evidence_file, sha256_file(str(evidence_file)))
        assert client.post(f"/api/evidence/{eid}/verify").status_code == 401
