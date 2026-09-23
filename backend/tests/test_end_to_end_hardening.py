"""Regressions found by walking the running system as an operator would.

Every test here failed before the fix that accompanies it. They are grouped by
the flow the failure surfaced in, not by module, because that is how each one
was actually found: a real login, a real search, a real camera delete.
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
    """`GET /api/self-heal/events` declared `Query(default=100, le=500)` — an
    upper bound with no lower one. SQLite reads `LIMIT -1` as NO LIMIT, so
    `?limit=-1` returned the entire self-heal event table to any authenticated
    user. `detections` and `review/queue` were given `ge=1` for exactly this
    reason (tests/test_list_limits.py); this endpoint was missed.
    """

    def test_a_negative_limit_is_rejected(self, client, auth):
        resp = client.get("/api/self-heal/events?limit=-1", headers=auth)
        assert resp.status_code == 422, "limit=-1 reaches SQLite as 'LIMIT -1', which means no limit at all"

    def test_zero_is_rejected(self, client, auth):
        assert client.get("/api/self-heal/events?limit=0", headers=auth).status_code == 422

    def test_a_normal_limit_still_works(self, client, auth):
        assert client.get("/api/self-heal/events?limit=5", headers=auth).status_code == 200


class TestUnboundedListEndpoints:
    """`GET /api/incidents` and `GET /api/evidence` ended in a bare `.all()`.

    Both tables grow with operational activity — one incident per CRITICAL
    alert, one evidence row per captured snapshot or clip — so "return every
    row" is a query whose cost rises forever, on two of the screens an
    operator opens most. Every other transactional list endpoint here is
    already capped (alerts 200, detections 100/500, audit 500).
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
    """A failed login writes the SUBMITTED username into the audit log's
    `resource` column, and nothing bounded it.

    Reproduced against the running system: one POST /api/auth/login carrying a
    5,000-character username — no account, no credential, no session needed —
    stored a 5,000-character audit row. Two consequences, both real:

      * The audit page renders every cell `whitespace-nowrap`, so that single
        row stretched the table to 36,215px wide. The screen an investigator
        uses to review activity was made unusable by input an unauthenticated
        attacker chose.
      * Storage growth from unauthenticated input is unbounded in the size
        dimension. The login rate limiter caps how MANY attempts are made; it
        never capped how LARGE what each attempt persists is.

    Truncated with an explicit marker rather than silently cut: an audit trail
    that quietly alters what it recorded is worse than one that says it
    shortened a value. Bounded inside `log_action` — the single funnel every
    audit write passes through — so the guarantee covers every caller, not
    only the login path that happens to be reachable without credentials.
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
    """The free-text pattern was built as an f-string around the operator's
    text and handed to `ilike` unescaped, so their own input was read as LIKE
    syntax.

    Measured on the running system: searching for a single percent sign
    returned 30 results — every camera in the database — from a query that
    matched nothing the operator typed. The underscore is the same problem one
    character at a time. In an investigative tool a search that silently
    widens itself is worse than one that returns nothing, because the extra
    rows look like findings.
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
    """Deleting a camera was blocked by zones the operator had already deleted.

    `DELETE /api/zones/{id}` is a soft delete (active=False) and `GET
    /api/zones` hides those rows. The camera-delete guard counted zones
    regardless of `active`, so an operator saw a camera with zero zones, tried
    to delete it, and was told it still held "1 zones" — with no screen
    anywhere that could show that zone, let alone remove it. An unreachable
    state: the instruction the error gives cannot be carried out.

    A zone is configuration, not evidence, and retiring one is already
    audited. A retired zone is therefore deleted along with its camera, while
    an ACTIVE zone still blocks — so the message always names something the
    operator can actually see and act on. The chain-of-custody records this
    guard exists for (detections, alerts, incidents, evidence, plates, tracks)
    are untouched and still block.
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
        """A rule holds a foreign key to the zone, so deleting the zone row out
        from under it would raise instead of answering. The rule is retired with
        the zone it can no longer evaluate."""
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
    """`DELETE /api/cameras/{id}` returned a raw 500 for almost every camera
    that had ever been started.

    `SelfHealEvent.camera_id` is a foreign key to `cameras.id`, and the
    delete guard's blocker list never counted it — so the request passed the
    guard, reached `DELETE FROM cameras`, and raised
    `IntegrityError: FOREIGN KEY constraint failed`. Found by deleting a probe
    camera on the running system; the camera had been started once, which is
    all it takes, because starting and stopping a camera writes self-heal
    events. On PostgreSQL, which has always enforced foreign keys, the failure
    is identical.

    This is the third time a table has been missed from a delete/wipe list in
    this codebase (BUG-C here, BUG-D in seed.reset_demo_data), so the fix is
    paired with `test_every_camera_foreign_key_is_accounted_for` below, which
    fails if a FUTURE table referencing `cameras.id` is added and neither
    blocked nor cleaned up.

    Self-heal events are per-camera operational telemetry — a recovery log,
    not chain-of-custody evidence and not operator configuration — and they
    are meaningless once the camera they describe is gone. They are therefore
    deleted with the camera, the same way retired zones are. Nothing in the
    evidence chain or the (separate, hash-chained) audit log is touched.
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
        """Structural guard against a fourth instance of this bug.

        Every mapped column that is a foreign key to `cameras.id` must either
        BLOCK deletion (operational history worth protecting) or be CLEANED UP
        with the camera. A new table that does neither passes review easily and
        then fails in production as a 500 on an ordinary admin action, which is
        exactly what happened here — so the check is derived from the mapper
        metadata rather than from a list someone has to remember to update.
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
    """Three identical in-force watchlist entries for one plate were creatable
    by clicking SAVE three times — reproduced against the running system.

    The operational failure is not the extra rows, it is what Deactivate then
    does: `plate_entry_in_force` takes `.first()`, so removing one of three
    identical entries leaves the plate exactly as watchlisted as before. The
    operator performs the documented removal action, watches the vehicle stay
    flagged, and has no way to tell why. This codebase already fixed the
    mirror image of that bug once — a stale `watchlist_flag` surviving
    deactivation.

    Refused with 409 naming the existing entry rather than silently
    de-duplicated: the operator may have meant to change the priority or the
    reason, and quietly discarding that input would hide it from them.
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
