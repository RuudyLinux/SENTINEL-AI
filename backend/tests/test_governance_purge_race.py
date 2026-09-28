"""Governance dry-run vs confirm, measured.

Worry: dry-run, time passes, confirm, and the retention boundary has moved,
so confirm deletes something the operator never saw. In fact confirm
re-evaluates in its own request and never replays the dry-run list, so the
only window is between query and delete inside one request.

Not enforced, on purpose (docs/PRIVACY_GOVERNANCE.md): legal hold. Evidence
on an open incident is purgeable once past retention. That's a policy call
for the operator; pinned below so it can't change unnoticed.
"""
import uuid
from datetime import datetime, timedelta

import pytest

from app import models
from app.config import settings


@pytest.fixture
def auth(admin_token):
    return {"Authorization": f"Bearer {admin_token}"}


@pytest.fixture(autouse=True)
def _restore_retention():
    original = settings.evidence_retention_days
    yield
    settings.evidence_retention_days = original


def _evidence(db, tmp_path, days_old: int, incident_status: str | None = None) -> models.Evidence:
    incident_id = None
    if incident_status is not None:
        incident = models.Incident(title=f"probe {uuid.uuid4().hex[:6]}", status=incident_status)
        db.add(incident)
        db.flush()
        incident_id = incident.id
    path = tmp_path / f"{uuid.uuid4().hex}.jpg"
    path.write_bytes(b"evidence bytes")
    row = models.Evidence(
        evidence_type="snapshot", file_path=str(path), verification_status="unverified",
        incident_id=incident_id, created_at=datetime.utcnow() - timedelta(days=days_old),
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


class TestConfirmRevalidates:
    def test_the_confirm_call_does_not_replay_a_stale_dry_run_list(self, client, db_session, auth, tmp_path):
        """Dry run lists what's eligible now; confirm acts on the world at
        confirm time, not the list the operator saw."""
        settings.evidence_retention_days = 30
        old = _evidence(db_session, tmp_path, days_old=90)

        dry = client.post("/api/governance/purge-expired", json={"dry_run": True}, headers=auth).json()
        assert old.id in dry["eligible_ids"]
        assert dry["deleted_count"] == 0

        # The world changes between the two calls: policy is widened so that
        # nothing is expired any more.
        settings.evidence_retention_days = 3650

        confirmed = client.post(
            "/api/governance/purge-expired", json={"dry_run": False, "confirm": True}, headers=auth,
        ).json()
        assert confirmed["deleted_count"] == 0, (
            "the confirm call deleted evidence based on the earlier dry-run's list — it must "
            "re-evaluate eligibility against the CURRENT retention policy."
        )
        assert db_session.query(models.Evidence).filter(models.Evidence.id == old.id).count() == 1

    def test_evidence_that_becomes_eligible_between_the_calls_is_caught_by_the_confirm(self, client, db_session, auth, tmp_path):
        """Other direction too, or confirm under-purges."""
        settings.evidence_retention_days = 3650
        old = _evidence(db_session, tmp_path, days_old=90)

        dry = client.post("/api/governance/purge-expired", json={"dry_run": True}, headers=auth).json()
        assert old.id not in dry["eligible_ids"]

        settings.evidence_retention_days = 30  # policy tightened after the dry-run

        confirmed = client.post(
            "/api/governance/purge-expired", json={"dry_run": False, "confirm": True}, headers=auth,
        ).json()
        assert old.id in confirmed["eligible_ids"]
        assert db_session.query(models.Evidence).filter(models.Evidence.id == old.id).count() == 0


class TestNoLegalHold:
    def test_evidence_on_an_open_incident_is_still_purged_when_expired(self, client, db_session, auth, tmp_path):
        """Documented gap, not a bug: retention, no legal hold. Evidence on
        an open investigation goes once past the configured retention; the
        retention period is the control. Pinned so it can't change quietly."""
        settings.evidence_retention_days = 30
        held = _evidence(db_session, tmp_path, days_old=90, incident_status="open")

        body = client.post(
            "/api/governance/purge-expired", json={"dry_run": False, "confirm": True}, headers=auth,
        ).json()

        assert held.id in body["eligible_ids"], (
            "behavior changed: evidence on an open incident is now retained. That may be an "
            "improvement, but it is a POLICY change — update docs/PRIVACY_GOVERNANCE.md, which "
            "currently states no legal-hold mechanism exists."
        )
