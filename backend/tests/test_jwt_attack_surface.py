"""Trying to break get_user_from_token/get_current_user with specific JWT
attacks, beyond the happy-path auth tests. All pass against the current
code; kept so a clean result is shown, not assumed.
"""
import base64
import json
from datetime import datetime, timedelta

from jose import jwt

from app.config import settings


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def test_expired_token_is_rejected(client, admin_user):
    expired = jwt.encode(
        {"sub": admin_user.id, "username": admin_user.username, "role": "Administrator",
         "exp": datetime.utcnow() - timedelta(hours=1)},
        settings.jwt_secret, algorithm=settings.jwt_algorithm,
    )
    resp = client.get("/api/auth/me", headers={"Authorization": f"Bearer {expired}"})
    assert resp.status_code == 401


def test_malformed_token_is_rejected(client):
    resp = client.get("/api/auth/me", headers={"Authorization": "Bearer not.a.jwt"})
    assert resp.status_code == 401


def test_wrong_signature_is_rejected(client, admin_user):
    """Valid-looking payload signed with a different secret, i.e. what a
    forger without JWT_SECRET would produce."""
    forged = jwt.encode(
        {"sub": admin_user.id, "exp": datetime.utcnow() + timedelta(hours=1)},
        "attacker-guessed-secret", algorithm=settings.jwt_algorithm,
    )
    resp = client.get("/api/auth/me", headers={"Authorization": f"Bearer {forged}"})
    assert resp.status_code == 401


def test_alg_none_token_is_rejected(client, admin_user):
    """alg: none. jose won't encode one, so it's built by hand to show the
    verify side rejects it too."""
    header = _b64url(json.dumps({"alg": "none", "typ": "JWT"}).encode())
    payload = _b64url(json.dumps({
        "sub": admin_user.id, "exp": (datetime.utcnow() + timedelta(hours=1)).timestamp(),
    }).encode())
    none_token = f"{header}.{payload}."  # empty signature segment
    resp = client.get("/api/auth/me", headers={"Authorization": f"Bearer {none_token}"})
    assert resp.status_code == 401


def test_wrong_algorithm_is_rejected(client, admin_user):
    """Validly signed with HS512 instead of the configured HS256; the
    algorithms allowlist must reject it."""
    token = jwt.encode(
        {"sub": admin_user.id, "exp": datetime.utcnow() + timedelta(hours=1)},
        settings.jwt_secret, algorithm="HS512",
    )
    resp = client.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 401


def test_very_long_garbage_token_is_rejected_without_delay(client):
    """A huge junk bearer value fails fast, no hang."""
    import time

    huge = "A" * 200_000
    started = time.monotonic()
    resp = client.get("/api/auth/me", headers={"Authorization": f"Bearer {huge}"})
    elapsed = time.monotonic() - started
    assert resp.status_code == 401
    assert elapsed < 2.0, f"rejecting a garbage token took {elapsed:.2f}s — too slow for a DoS-resistant auth check"
