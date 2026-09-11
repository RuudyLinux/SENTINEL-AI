"""Opt-in plate redaction for evidence packages (workstream C5).

`docs/PRIVACY_GOVERNANCE.md` previously listed export redaction as NOT
provided. The reason it needed care rather than a one-line mask: the
registration reaches an evidence package through FIVE independent paths —

    vehicle.plate_text
    alert.reasons            ("Watchlist signal: plate GJ05AB1234 matches ...")
    incident.title           ("Potential match - GJ05AB1234 on C-014")
    incident.description     (built by joining those reasons)
    audit_trail[].resource   (a watchlist entry's resource IS the plate)

so masking the obvious field yields a document that LOOKS redacted while
still disclosing the plate three other ways — worse than no redaction,
because the reader trusts it. These tests assert the plate is absent from
the ENTIRE serialized document, and that integrity data survives.
"""
import json
import uuid

import pytest

from app import models


@pytest.fixture
def auth(admin_token):
    return {"Authorization": f"Bearer {admin_token}"}


@pytest.fixture
def incident_with_plate_everywhere(db_session):
    """Builds an incident whose plate appears in all five leak paths."""
    plate = f"GJ05RD{uuid.uuid4().hex[:4].upper()}"
    camera = models.Camera(
        camera_code=f"RED-{uuid.uuid4().hex[:8]}", name="redaction probe",
        source_type="video_file", source_uri="x.mp4",
    )
    db_session.add(camera)
    db_session.flush()
    vehicle = models.Vehicle(plate_text=plate, plate_confidence=0.93, watchlist_flag=True)
    db_session.add(vehicle)
    db_session.flush()
    detection = models.Detection(camera_id=camera.id, cls="car", confidence=0.9, bbox=[1, 1, 50, 50])
    db_session.add(detection)
    db_session.flush()
    alert = models.Alert(
        camera_id=camera.id, severity="CRITICAL", vehicle_id=vehicle.id, detection_id=detection.id,
        reasons=[f"Watchlist signal: plate {plate} matches an active watchlist entry"],
    )
    db_session.add(alert)
    db_session.flush()
    incident = models.Incident(
        title=f"Potential match - {plate} on {camera.camera_code}",
        description=f"Watchlist signal: plate {plate} matches an active watchlist entry",
        camera_id=camera.id, alert_id=alert.id, vehicle_id=vehicle.id, status="open",
    )
    db_session.add(incident)
    db_session.flush()
    evidence = models.Evidence(
        incident_id=incident.id, camera_id=camera.id, alert_id=alert.id, detection_id=detection.id,
        evidence_type="snapshot", sha256="ab" * 32, verification_status="verified",
    )
    db_session.add(evidence)
    db_session.commit()
    return {"incident_id": incident.id, "plate": plate, "evidence_id": evidence.id, "sha256": evidence.sha256}


def _package(client, auth, incident_id: str, redact: bool) -> dict:
    token = client.get(f"/api/evidence/incidents/{incident_id}/package-token", headers=auth).json()["token"]
    url = f"/api/evidence/incidents/{incident_id}/package?token={token}&fmt=json"
    if redact:
        url += "&redact=true"
    resp = client.get(url)
    assert resp.status_code == 200
    return json.loads(resp.content)


class TestRedaction:
    def test_the_plate_appears_nowhere_in_a_redacted_package(self, client, auth, incident_with_plate_everywhere):
        """The whole point: search the ENTIRE serialized document, not
        selected fields."""
        data = incident_with_plate_everywhere
        package = _package(client, auth, data["incident_id"], redact=True)

        blob = json.dumps(package)
        assert data["plate"] not in blob, (
            "the registration survived redaction somewhere in the package — "
            "field-by-field masking missed a path"
        )

    def test_an_unredacted_package_still_contains_the_plate(self, client, auth, incident_with_plate_everywhere):
        """Redaction is OPT-IN. The default export is the evidentiary
        artefact and must not be silently degraded."""
        data = incident_with_plate_everywhere
        blob = json.dumps(_package(client, auth, data["incident_id"], redact=False))
        assert data["plate"] in blob

    def test_redaction_masks_but_keeps_the_plate_correlatable(self, client, auth, incident_with_plate_everywhere):
        data = incident_with_plate_everywhere
        package = _package(client, auth, data["incident_id"], redact=True)
        masked = package["vehicle"]["plate_text"]
        assert masked.startswith(data["plate"][:2])
        assert masked.endswith(data["plate"][-2:])
        assert "*" in masked

    def test_integrity_data_is_never_redacted(self, client, auth, incident_with_plate_everywhere):
        """A redacted package must remain verifiable against the source
        evidence, so ids, digests and verification status pass through."""
        data = incident_with_plate_everywhere
        package = _package(client, auth, data["incident_id"], redact=True)
        item = next(e for e in package["evidence"] if e["id"] == data["evidence_id"])
        assert item["sha256"] == data["sha256"]
        assert item["verification_status"] == "verified"

    def test_a_redacted_package_declares_itself(self, client, auth, incident_with_plate_everywhere):
        """A reader must be able to tell they are holding a partial export."""
        data = incident_with_plate_everywhere
        package = _package(client, auth, data["incident_id"], redact=True)
        assert package["redaction"]["applied"] is True
        assert "scheme" in package["redaction"]

    def test_which_export_was_produced_is_audited(self, client, auth, db_session, incident_with_plate_everywhere):
        """A redacted export and a full one are different disclosures of the
        same incident — the audit trail has to distinguish them."""
        data = incident_with_plate_everywhere
        _package(client, auth, data["incident_id"], redact=True)

        actions = {
            row.action for row in db_session.query(models.AuditLog).filter(
                models.AuditLog.resource == data["incident_id"]
            ).all()
        }
        assert "generate_evidence_package_redacted" in actions

    def test_an_incident_with_no_vehicle_redacts_without_error(self, client, auth, db_session):
        """Nothing to mask must not be an error path."""
        camera = models.Camera(
            camera_code=f"RED-{uuid.uuid4().hex[:8]}", name="no vehicle",
            source_type="video_file", source_uri="x.mp4",
        )
        db_session.add(camera)
        db_session.flush()
        incident = models.Incident(title="zone entry, no vehicle", camera_id=camera.id, status="open")
        db_session.add(incident)
        db_session.commit()

        package = _package(client, auth, incident.id, redact=True)
        assert package["vehicle"] is None
