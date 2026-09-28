"""The login limiter (routers/auth.py) was keyed by the attacker-supplied
username and never evicted, so every failed login added an entry for good:

  - 500 distinct usernames -> 500 keys kept forever
  - one 1,000,000-char username -> one 1,000,049-byte key kept forever

Unauthenticated remote memory growth. These pin both bounds (count and size)
and that rate limiting itself still works.
"""
import sys

import pytest

from app.routers import auth as auth_router


@pytest.fixture(autouse=True)
def _clean_limiter():
    auth_router._failed_attempts.clear()
    yield
    auth_router._failed_attempts.clear()


class TestBoundedMemory:
    def test_distinct_usernames_do_not_grow_the_table_without_bound(self, client):
        """Spraying new usernames doesn't keep one entry each forever."""
        for i in range(auth_router._LOGIN_MAX_TRACKED_USERNAMES * 3):
            resp = client.post("/api/auth/login", json={"username": f"sprayer-{i}", "password": "x"})
            assert resp.status_code == 401

        assert len(auth_router._failed_attempts) <= auth_router._LOGIN_MAX_TRACKED_USERNAMES, (
            f"login limiter retained {len(auth_router._failed_attempts)} entries — "
            "unbounded growth from attacker-controlled usernames (BUG-A regressed)."
        )

    def test_an_enormous_username_cannot_become_an_enormous_key(self, client):
        huge = "A" * 1_000_000
        resp = client.post("/api/auth/login", json={"username": huge, "password": "x"})
        assert resp.status_code == 401

        largest = max((sys.getsizeof(k) for k in auth_router._failed_attempts), default=0)
        assert largest < 4096, (
            f"login limiter stored a {largest}-byte key — an attacker-supplied username "
            "is being retained verbatim (BUG-A regressed)."
        )


class TestTargetedLockoutIsNotPossible:
    """Keyed by (username, source IP). Username only let anyone lock out
    `admin` (it's in the README) from anywhere with five wrong passwords, an
    availability attack worse than the brute force it stops."""

    def test_an_attacker_cannot_lock_out_an_account_for_a_different_source(self):
        """Tested on the limiter directly; TestClient uses one client host for
        everything, so different addresses can't be faked over HTTP."""
        auth_router._failed_attempts.clear()
        for _ in range(auth_router._LOGIN_MAX_ATTEMPTS + 2):
            auth_router._record_failed_attempt("admin", ip="203.0.113.9")

        assert auth_router._rate_limited("admin", ip="203.0.113.9") is True
        assert auth_router._rate_limited("admin", ip="198.51.100.4") is False, (
            "a different source address was locked out of the same account — "
            "targeted account lockout is still possible"
        )

    def test_the_attacking_source_itself_is_still_limited(self):
        """The address doing the guessing still gets cut off for that account."""
        auth_router._failed_attempts.clear()
        for _ in range(auth_router._LOGIN_MAX_ATTEMPTS):
            auth_router._record_failed_attempt("admin", ip="203.0.113.9")
        assert auth_router._rate_limited("admin", ip="203.0.113.9") is True

    def test_one_source_guessing_one_account_does_not_limit_its_other_accounts(self):
        auth_router._failed_attempts.clear()
        for _ in range(auth_router._LOGIN_MAX_ATTEMPTS):
            auth_router._record_failed_attempt("admin", ip="203.0.113.9")
        assert auth_router._rate_limited("operator1", ip="203.0.113.9") is False


class TestRateLimitingStillWorks:
    """The bounds above must not weaken the actual brute-force protection."""

    def test_repeated_failures_for_one_username_still_lock_out(self, client):
        for _ in range(auth_router._LOGIN_MAX_ATTEMPTS):
            assert client.post("/api/auth/login", json={"username": "admin", "password": "wrong"}).status_code == 401

        locked = client.post("/api/auth/login", json={"username": "admin", "password": "wrong"})
        assert locked.status_code == 429

    def test_a_successful_login_clears_the_failure_record(self, client, db_session, admin_user):
        from app.security import hash_password

        admin_user.password_hash = hash_password("realpassword123")
        db_session.commit()

        for _ in range(auth_router._LOGIN_MAX_ATTEMPTS - 1):
            client.post("/api/auth/login", json={"username": admin_user.username, "password": "wrong"})

        ok = client.post("/api/auth/login", json={"username": admin_user.username, "password": "realpassword123"})
        assert ok.status_code == 200
        # Cleared, so the next failure starts a fresh window rather than
        # immediately tripping the limit.
        assert client.post(
            "/api/auth/login", json={"username": admin_user.username, "password": "wrong"}
        ).status_code == 401

    def test_one_username_being_limited_never_locks_out_a_different_one(self, client):
        for _ in range(auth_router._LOGIN_MAX_ATTEMPTS + 2):
            client.post("/api/auth/login", json={"username": "victim", "password": "wrong"})
        assert client.post("/api/auth/login", json={"username": "victim", "password": "wrong"}).status_code == 429

        # A different account must still be able to attempt (and fail) normally.
        assert client.post("/api/auth/login", json={"username": "someone-else", "password": "wrong"}).status_code == 401

    def test_eviction_never_drops_a_username_that_is_actively_being_attacked(self, client):
        """Stale entries go first. An account under active attack mustn't get
        evicted by spraying other usernames, that'd be a bypass."""
        for _ in range(auth_router._LOGIN_MAX_ATTEMPTS):
            client.post("/api/auth/login", json={"username": "target", "password": "wrong"})
        assert client.post("/api/auth/login", json={"username": "target", "password": "wrong"}).status_code == 429

        for i in range(auth_router._LOGIN_MAX_TRACKED_USERNAMES * 2):
            client.post("/api/auth/login", json={"username": f"noise-{i}", "password": "x"})

        still_limited = client.post("/api/auth/login", json={"username": "target", "password": "wrong"})
        assert still_limited.status_code == 429, (
            "an actively-attacked username's rate limit was evicted by username spraying — "
            "the eviction policy is a rate-limit bypass."
        )
