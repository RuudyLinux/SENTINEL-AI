"""Tamper-evident audit chain (10/10 roadmap P9): app/audit.py's hash chain,
and the GET /api/audit/verify-chain endpoint that walks it."""
import pytest

from app import audit, models


@pytest.fixture
def auth(admin_token):
    return {"Authorization": f"Bearer {admin_token}"}


class TestLogActionChaining:
    def test_first_entry_chains_from_genesis(self, db_session):
        # Isolate: only assert properties of the row we just wrote, never
        # "this is the first row ever" (the suite shares one DB across files).
        entry = audit.log_action(db_session, None, "test_action_genesis")
        assert entry is not None
        assert entry.chain_seq is not None
        assert entry.entry_hash == audit.compute_entry_hash(entry, entry.prev_hash)

    def test_consecutive_entries_link_prev_hash_to_entry_hash(self, db_session):
        first = audit.log_action(db_session, None, "test_action_link_a")
        second = audit.log_action(db_session, None, "test_action_link_b")
        assert second.chain_seq == first.chain_seq + 1
        assert second.prev_hash == first.entry_hash

    def test_entry_hash_changes_if_any_covered_field_changes(self, db_session):
        entry = audit.log_action(db_session, None, "test_action_hash_sensitivity", resource="r1")
        original_hash = entry.entry_hash
        other = models.AuditLog(
            id="aud_fakefakefak", user_id=None, username="anonymous",
            action="test_action_hash_sensitivity", resource="DIFFERENT",
            result="SUCCESS", ip="", timestamp=entry.timestamp,
            chain_seq=entry.chain_seq, prev_hash=entry.prev_hash,
        )
        assert audit.compute_entry_hash(other, entry.prev_hash) != original_hash


class TestVerifyChain:
    def test_intact_chain_verifies(self, db_session):
        audit.log_action(db_session, None, "test_action_verify_1")
        audit.log_action(db_session, None, "test_action_verify_2")
        result = audit.verify_chain(db_session)
        assert result["valid"] is True
        assert result["broken_at"] is None

    def test_a_modified_row_is_detected(self, db_session):
        audit.log_action(db_session, None, "test_action_tamper_before")
        victim = audit.log_action(db_session, None, "test_action_tamper_target")
        audit.log_action(db_session, None, "test_action_tamper_after")

        # Simulate tampering: mutate a field the hash covers, directly in the DB.
        original_resource = victim.resource
        victim.resource = "TAMPERED-BY-TEST"
        db_session.commit()

        try:
            result = audit.verify_chain(db_session)
            assert result["valid"] is False
            assert result["broken_at"] == victim.chain_seq
        finally:
            # The chain is a single global, append-only log shared by every
            # test in this suite (and by test_a_deleted_row_... below) — leave
            # it intact rather than permanently poisoning verify_chain for
            # every test that runs after this one.
            victim.resource = original_resource
            db_session.commit()

    def test_a_deleted_row_breaks_the_chain_for_everything_after_it(self, db_session):
        audit.log_action(db_session, None, "test_action_delete_before")
        victim = audit.log_action(db_session, None, "test_action_delete_target")
        after = audit.log_action(db_session, None, "test_action_delete_after")
        # Snapshot every field verify_chain depends on, so the row can be
        # restored afterward — this table is one global, append-only log
        # shared by the rest of the suite; a real deletion left in place
        # would permanently poison verify_chain for every test after this one.
        victim_fields = {
            "id": victim.id, "user_id": victim.user_id, "username": victim.username,
            "action": victim.action, "resource": victim.resource, "result": victim.result,
            "ip": victim.ip, "timestamp": victim.timestamp, "chain_seq": victim.chain_seq,
            "prev_hash": victim.prev_hash, "entry_hash": victim.entry_hash,
        }

        db_session.delete(victim)
        db_session.commit()

        try:
            result = audit.verify_chain(db_session)
            assert result["valid"] is False
            assert result["broken_at"] == after.chain_seq
        finally:
            db_session.add(models.AuditLog(**victim_fields))
            db_session.commit()

    def test_verify_chain_endpoint_requires_admin_or_auditor_role(self, client, auth):
        resp = client.get("/api/audit/verify-chain", headers=auth)
        assert resp.status_code == 200
        assert "valid" in resp.json()

    def test_verify_chain_endpoint_requires_auth(self, client):
        assert client.get("/api/audit/verify-chain").status_code == 401
