"""Final deep-debug pass — BUG-5 (the governance dry-run/confirm boundary),
resolved by measurement rather than left as a vague "accepted".

The stated concern was: dry-run -> time passes -> confirm -> the retention
boundary has moved, so the confirm deletes something the operator never saw
listed. These tests establish what the code ACTUALLY does at that boundary:
the confirm call re-evaluates eligibility inside its own request and never
replays the dry-run's list, so the only window is the microseconds between
query and delete WITHIN one request — not the operator's think-time.

What is still NOT enforced (unchanged, and deliberately so — see
docs/PRIVACY_GOVERNANCE.md): there is no legal hold. Evidence attached to a
still-open incident IS eligible for purge once it ages past the configured
retention. That is an operator/policy responsibility, not something this
platform decides silently; the test below pins that behavior so it cannot
change by accident without someone noticing.
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
        """The heart of BUG-5: a dry-run lists what is eligible NOW; if the
        world changes before the operator confirms, the confirm must act on
        the CURRENT world, not the snapshot the operator saw."""
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
        """The mirror case: re-evaluation must work in both directions, or
        the confirm silently under-purges relative to the stated policy."""
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
        """Pins a DOCUMENTED, deliberate gap rather than a discovered bug:
        this platform implements retention, not legal hold. Evidence on an
        open investigation is purged once it ages past the operator's
        configured retention. If that is wrong for a deployment, the
        retention period is the control — see docs/PRIVACY_GOVERNANCE.md.
        This test exists so the behavior cannot change unnoticed in either
        direction."""
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
