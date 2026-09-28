"""Regressions found by using the running system like an operator.

Each failed before its fix. Grouped by the flow where it showed up (a login,
a search, a camera delete), not by module.
"""
import uuid
from datetime import datetime, timedelta

import pytest

from app import models
from app.audit import MAX_AUDIT_RESOURCE_CHARS, log_action


@pytest.fixture
def auth(admin_token):
    return {"Authorization": f"Bearer {admin_token}"}


class TestSelfHealEventsLimit:
    """/api/self-heal/events had le=500 but no ge, and SQLite reads LIMIT -1
    as no limit, so ?limit=-1 returned the whole table to any logged-in user.
    detections and review/queue already had ge=1 (tests/test_list_limits.py).
    """

    def test_a_negative_limit_is_rejected(self, client, auth):
        resp = client.get("/api/self-heal/events?limit=-1", headers=auth)
        assert resp.status_code == 422, "limit=-1 reaches SQLite as 'LIMIT -1', which means no limit at all"

    def test_zero_is_rejected(self, client, auth):
        assert client.get("/api/self-heal/events?limit=0", headers=auth).status_code == 422

    def test_a_normal_limit_still_works(self, client, auth):
        assert client.get("/api/self-heal/events?limit=5", headers=auth).status_code == 200


class TestUnboundedListEndpoints:
    """/api/incidents and /api/evidence ended in a bare .all(). Both grow
    with activity and are opened constantly; everything else was already
    capped (alerts 200, detections 100/500, audit 500).
    """

    def test_incidents_are_capped(self, client, auth, db_session):
        for i in range(12):
            db_session.add(models.Incident(title=f"bulk incident {i}", status="open"))
        db_session.commit()
        rows = client.get("/api/incidents?limit=5", headers=auth).json()
        assert len(rows) == 5

    def test_incidents_reject_an_unbounded_limit(self, client, auth):
        assert client.get("/api/incidents?limit=-1", headers=auth).status_code == 422
        assert client.get("/api/incidents?limit=100000000", headers=auth).status_code == 422

    def test_evidence_rejects_an_unbounded_limit(self, client, auth):
        assert client.get("/api/evidence?limit=-1", headers=auth).status_code == 422
        assert client.get("/api/evidence?limit=100000000", headers=auth).status_code == 422

    def test_evidence_honours_a_limit(self, client, auth, db_session):
        for i in range(6):
            db_session.add(models.Evidence(evidence_type="snapshot", file_path=f"/tmp/e{i}.jpg", event_type="test"))
        db_session.commit()
        rows = client.get("/api/evidence?limit=3", headers=auth).json()
        assert len(rows) == 3


class TestAuditResourceIsBounded:
    """A failed login writes the submitted username into the audit
    `resource`, with no cap.

    One login with a 5,000-char username (no account needed) stored a
    5,000-char row, and the audit table (whitespace-nowrap) went 36,215px
    wide. The rate limiter caps how many attempts, not how big each row is.

    Cut with a visible marker, not silently. Done in log_action, which every
    audit write goes through, so it covers all callers.
    """

    def test_a_long_resource_is_truncated(self, db_session, admin_user):
        entry = log_action(db_session, admin_user, "test_action", resource="x" * 9000)
        assert len(entry.resource) <= MAX_AUDIT_RESOURCE_CHARS
        assert entry.resource.endswith("...[truncated]"), "a shortened value must say so"

    def test_a_normal_resource_is_stored_verbatim(self, db_session, admin_user):
        entry = log_action(db_session, admin_user, "test_action", resource="cam_0123456789")
        assert entry.resource == "cam_0123456789"

    def test_a_long_username_at_login_cannot_write_an_unbounded_row(self, client, db_session):
        client.post("/api/auth/login", json={"username": "z" * 9000, "password": "nope"})
        row = (
            db_session.query(models.AuditLog)
            .filter(models.AuditLog.action == "login_failed")
            .order_by(models.AuditLog.timestamp.desc())
            .first()
        )
        assert row is not None
        assert len(row.resource) <= MAX_AUDIT_RESOURCE_CHARS

    def test_the_hash_chain_still_verifies_after_truncation(self, client, db_session, auth):
        log_action(db_session, None, "test_action", resource="y" * 9000)
        assert client.get("/api/audit/verify-chain", headers=auth).json()["valid"] is True


class TestSearchWildcardsAreLiteral:
    """The search pattern was an f-string around the operator's text, so
    their input was LIKE syntax: a lone % returned all 30 cameras, and _ did
    the same one character at a time. A search that widens itself is worse
    than one that finds nothing, the extra rows look like findings.
    """

    @pytest.fixture
    def cameras(self, db_session):
        suffix = uuid.uuid4().hex[:6]
        for name in (f"Ring Road {suffix}", f"100% Junction {suffix}", f"Under_Bridge {suffix}"):
            db_session.add(models.Camera(
                camera_code=f"SRCH-{uuid.uuid4().hex[:6]}", name=name,
                source_type="mock_vms", source_uri="",
            ))
        db_session.commit()
        return suffix

    def test_a_bare_percent_is_not_a_wildcard(self, client, auth, cameras):
        rows = client.get("/api/search", params={"q": "%"}, headers=auth).json()["cameras"]
        assert all("%" in c["name"] for c in rows), "a literal % must not match every camera"

    def test_an_underscore_is_not_a_single_character_wildcard(self, client, auth, cameras):
        rows = client.get("/api/search", params={"q": "Under_Bridge"}, headers=auth).json()["cameras"]
        assert rows, "the camera whose name really contains an underscore must still be found"
        assert all("Under_Bridge" in c["name"] for c in rows)

    def test_ordinary_text_search_is_unchanged(self, client, auth, cameras):
        rows = client.get("/api/search", params={"q": f"Ring Road {cameras}"}, headers=auth).json()["cameras"]
        assert any("Ring Road" in c["name"] for c in rows)


class TestCameraDeleteIgnoresRetiredZones:
    """Zones the operator already deleted blocked deleting the camera.

    Zone delete is soft and the zone list hides those rows, but the camera
    guard counted them, so a camera showing no zones said it still held
    "1 zones" that no screen could show or remove.

    Retired zones (config, already audited) go with the camera; active ones
    still block. Detections, alerts, incidents, evidence, plates and tracks
    still block too.
    """

    @pytest.fixture
    def camera(self, db_session):
        cam = models.Camera(
            camera_code=f"ZDEL-{uuid.uuid4().hex[:6]}", name="zone delete cam",
            source_type="mock_vms", source_uri="",
        )
        db_session.add(cam)
        db_session.commit()
        return cam

    def test_an_active_zone_still_blocks_deletion(self, client, auth, db_session, camera):
        db_session.add(models.Zone(camera_id=camera.id, name="live zone", active=True))
        db_session.commit()
        resp = client.delete(f"/api/cameras/{camera.id}", headers=auth)
        assert resp.status_code == 409
        assert "zones" in resp.json()["detail"]

    def test_a_retired_zone_does_not_block_deletion(self, client, auth, db_session, camera):
        db_session.add(models.Zone(camera_id=camera.id, name="retired zone", active=False))
        db_session.commit()
        assert client.delete(f"/api/cameras/{camera.id}", headers=auth).status_code == 200

    def test_the_retired_zone_row_goes_with_the_camera(self, client, auth, db_session, camera):
        zone = models.Zone(camera_id=camera.id, name="retired zone", active=False)
        db_session.add(zone)
        db_session.commit()
        zone_id = zone.id
        client.delete(f"/api/cameras/{camera.id}", headers=auth)
        db_session.expire_all()
        assert db_session.query(models.Zone).filter(models.Zone.id == zone_id).first() is None, (
            "leaving the row behind would be a dangling foreign key once the camera is gone"
        )

    def test_a_rule_attached_to_a_retired_zone_does_not_500(self, client, auth, db_session, camera):
        """A rule has a FK to the zone, so it's retired along with the zone
        it can no longer evaluate."""
        zone = models.Zone(camera_id=camera.id, name="retired zone", active=False)
        db_session.add(zone)
        db_session.flush()
        rule = models.AlertRule(name="orphan rule", rule_type="zone_entry", zone_id=zone.id)
        db_session.add(rule)
        db_session.commit()
        rule_id = rule.id
        assert client.delete(f"/api/cameras/{camera.id}", headers=auth).status_code == 200
        db_session.expire_all()
        assert db_session.query(models.AlertRule).filter(models.AlertRule.id == rule_id).first() is None

    def test_real_history_still_blocks_deletion(self, client, auth, db_session, camera):
        db_session.add(models.Detection(camera_id=camera.id, cls="car", confidence=0.9, bbox=[1, 2, 3, 4]))
        db_session.commit()
        resp = client.delete(f"/api/cameras/{camera.id}", headers=auth)
        assert resp.status_code == 409
        assert "detections" in resp.json()["detail"]


class TestCameraDeleteSelfHealEvents:
    """DELETE /api/cameras/{id} was a raw 500 for nearly every camera that had
    ever been started.

    SelfHealEvent.camera_id is a FK to cameras.id and the guard didn't count
    it, so the delete hit "FOREIGN KEY constraint failed". Starting and
    stopping a camera writes self-heal events, so one start was enough.
    Same on PostgreSQL.

    Third time a table was missed from a delete list, hence
    test_every_camera_foreign_key_is_accounted_for below. Self-heal events
    are per-camera telemetry, not evidence or config, so they're deleted
    with the camera like retired zones. The evidence chain and audit log
    aren't touched.
    """

    @pytest.fixture
    def camera(self, db_session):
        cam = models.Camera(
            camera_code=f"SHE-{uuid.uuid4().hex[:6]}", name="self heal cam",
            source_type="mock_vms", source_uri="",
        )
        db_session.add(cam)
        db_session.commit()
        return cam

    def test_a_camera_with_self_heal_events_can_be_deleted(self, client, auth, db_session, camera):
        db_session.add(models.SelfHealEvent(
            component="camera", camera_id=camera.id, error_type="connection_lost",
            status="RECOVERED", severity="warning", message="reconnected",
        ))
        db_session.commit()
        resp = client.delete(f"/api/cameras/{camera.id}", headers=auth)
        assert resp.status_code == 200, f"expected a clean delete, got {resp.status_code}: {resp.text[:200]}"

    def test_the_events_go_with_the_camera(self, client, auth, db_session, camera):
        event = models.SelfHealEvent(
            component="camera", camera_id=camera.id, error_type="connection_lost",
            status="RECOVERED", severity="warning", message="reconnected",
        )
        db_session.add(event)
        db_session.commit()
        event_id = event.id
        client.delete(f"/api/cameras/{camera.id}", headers=auth)
        db_session.expire_all()
        assert db_session.query(models.SelfHealEvent).filter(models.SelfHealEvent.id == event_id).first() is None

    def test_events_for_other_cameras_are_untouched(self, client, auth, db_session, camera):
        other = models.Camera(
            camera_code=f"SHE-{uuid.uuid4().hex[:6]}", name="bystander",
            source_type="mock_vms", source_uri="",
        )
        db_session.add(other)
        db_session.flush()
        keep = models.SelfHealEvent(
            component="camera", camera_id=other.id, error_type="connection_lost",
            status="RECOVERED", severity="warning", message="not mine to delete",
        )
        db_session.add(keep)
        db_session.add(models.SelfHealEvent(
            component="camera", camera_id=camera.id, error_type="connection_lost",
            status="RECOVERED", severity="warning", message="mine",
        ))
        db_session.commit()
        keep_id = keep.id
        client.delete(f"/api/cameras/{camera.id}", headers=auth)
        db_session.expire_all()
        assert db_session.query(models.SelfHealEvent).filter(models.SelfHealEvent.id == keep_id).first() is not None

    def test_every_camera_foreign_key_is_accounted_for(self):
        """Every FK to cameras.id must either block deletion or be cleaned up
        with the camera. Derived from mapper metadata so a new table can't
        slip by the way SelfHealEvent did.
        """
        from app.routers.cameras import CAMERA_BLOCKER_MODELS, CAMERA_CASCADE_MODELS

        handled = {m.__tablename__ for m in CAMERA_BLOCKER_MODELS.values()} | {
            m.__tablename__ for m in CAMERA_CASCADE_MODELS
        }
        referencing = set()
        for mapper in models.Base.registry.mappers:
            for column in mapper.class_.__table__.columns:
                for fk in column.foreign_keys:
                    if fk.column.table.name == "cameras":
                        referencing.add(mapper.class_.__tablename__)
        missing = referencing - handled
        assert not missing, (
            f"tables reference cameras.id but are neither blocked nor cleaned up on camera delete: {sorted(missing)} "
            "— deleting a camera will raise a raw IntegrityError (500) for any camera holding such a row"
        )


class TestWatchlistDuplicateEntries:
    """SAVE clicked three times made three identical in-force entries for one
    plate.

    The problem is Deactivate afterwards: plate_entry_in_force takes .first(),
    so removing one of three leaves the plate flagged, and the operator can't
    tell why.

    409 naming the existing entry instead of silently de-duplicating; they
    may have meant to change the priority or reason.
    """

    @pytest.fixture
    def plate(self):
        return f"GJ01XX{uuid.uuid4().int % 10000:04d}"

    def _create(self, client, auth, plate, **kw):
        body = {"entity_type": "plate", "identifier": plate, "reason": "test", "priority": "HIGH"}
        body.update(kw)
        return client.post("/api/watchlists", json=body, headers=auth)

    def test_the_first_entry_is_created(self, client, auth, plate):
        assert self._create(client, auth, plate).status_code == 200

    def test_a_duplicate_in_force_entry_is_refused(self, client, auth, plate):
        self._create(client, auth, plate)
        resp = self._create(client, auth, plate)
        assert resp.status_code == 409
        assert plate in resp.json()["detail"]

    def test_deactivating_then_re_adding_works(self, client, auth, plate):
        entry_id = self._create(client, auth, plate).json()["id"]
        assert client.delete(f"/api/watchlists/{entry_id}", headers=auth).status_code == 200
        assert self._create(client, auth, plate).status_code == 200, (
            "a plate removed from the watchlist must be re-addable"
        )

    def test_an_expired_entry_does_not_block_a_new_one(self, client, auth, plate, db_session):
        db_session.add(models.WatchlistEntry(
            entity_type="plate", identifier=plate, reason="expired",
            priority="HIGH", valid_until=datetime.utcnow() - timedelta(days=1),
        ))
        db_session.commit()
        assert self._create(client, auth, plate).status_code == 200

    def test_the_same_identifier_under_a_different_entity_type_is_allowed(self, client, auth, plate):
        self._create(client, auth, plate)
        assert self._create(client, auth, plate, entity_type="person").status_code == 200
