"""10/10 debugging pass — active JWT attack-surface probing (not just the
happy-path auth tests elsewhere). Every case here is a real attempt to break
`security.get_user_from_token`/`get_current_user` with a specific, named
attack, not a generic "auth works" smoke test. All six PASS against the
existing implementation — recorded as "attacked, no bug found", per the
debugging pass's own rule that a clean result must be demonstrated, not
assumed.
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
    """A token whose payload looks valid but was signed with a DIFFERENT
    secret — the exact shape of a forged token an attacker without the real
    JWT_SECRET would have to produce."""
    forged = jwt.encode(
        {"sub": admin_user.id, "exp": datetime.utcnow() + timedelta(hours=1)},
        "attacker-guessed-secret", algorithm=settings.jwt_algorithm,
    )
    resp = client.get("/api/auth/me", headers={"Authorization": f"Bearer {forged}"})
    assert resp.status_code == 401


def test_alg_none_token_is_rejected(client, admin_user):
    """The classic `alg: none` JWT attack — a token that claims to require no
    signature verification at all. `jose.jwt.encode` itself refuses to
    produce one (`ALGORITHMS.SUPPORTED` excludes "none"), so it is built by
    hand here to prove the VERIFY side rejects it too, not just that this
    library's own signer is safe."""
    header = _b64url(json.dumps({"alg": "none", "typ": "JWT"}).encode())
    payload = _b64url(json.dumps({
        "sub": admin_user.id, "exp": (datetime.utcnow() + timedelta(hours=1)).timestamp(),
    }).encode())
    none_token = f"{header}.{payload}."  # empty signature segment
    resp = client.get("/api/auth/me", headers={"Authorization": f"Bearer {none_token}"})
    assert resp.status_code == 401


def test_wrong_algorithm_is_rejected(client, admin_user):
    """A validly-signed token, but with HS512 instead of the configured
    HS256 (`settings.jwt_algorithm`) — `jwt.decode`'s `algorithms=[...]`
    allowlist must reject an otherwise-valid signature using an algorithm
    the server never agreed to accept."""
    token = jwt.encode(
        {"sub": admin_user.id, "exp": datetime.utcnow() + timedelta(hours=1)},
        settings.jwt_secret, algorithm="HS512",
    )
    resp = client.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 401


def test_very_long_garbage_token_is_rejected_without_delay(client):
    """Resource-exhaustion angle: an oversized, meaningless bearer value must
    fail fast (decode error), not hang or consume unbounded resources trying
    to parse it."""
    import time

    huge = "A" * 200_000
    started = time.monotonic()
    resp = client.get("/api/auth/me", headers={"Authorization": f"Bearer {huge}"})
    elapsed = time.monotonic() - started
    assert resp.status_code == 401
    assert elapsed < 2.0, f"rejecting a garbage token took {elapsed:.2f}s — too slow for a DoS-resistant auth check"
