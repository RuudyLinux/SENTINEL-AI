"""Hardening pass: DEMO_MODE=false (production mode) must reject an
insecure/default JWT secret at startup rather than silently signing real
tokens with a publicly-known dev value. Demo mode itself must stay
completely unaffected."""
import pytest

from app.config import Settings


def test_production_mode_rejects_the_bundled_dev_secret():
    with pytest.raises(RuntimeError, match="JWT_SECRET"):
        Settings(demo_mode=False, jwt_secret="sentinel-vision-dev-secret-change-in-production")


def test_production_mode_rejects_a_short_or_placeholder_secret():
    with pytest.raises(RuntimeError, match="JWT_SECRET"):
        Settings(demo_mode=False, jwt_secret="changeme")
    with pytest.raises(RuntimeError, match="JWT_SECRET"):
        Settings(demo_mode=False, jwt_secret="short")


def test_production_mode_accepts_a_real_generated_secret():
    real_secret = "x" * 40  # stands in for secrets.token_urlsafe(32) output
    s = Settings(demo_mode=False, jwt_secret=real_secret)
    assert s.jwt_secret == real_secret


def test_demo_mode_starts_without_a_secret_but_never_signs_with_the_public_one():
    # Demo mode must still start with no JWT_SECRET configured, but the
    # bundled secret is in this repository, so signing with it would let
    # anyone mint an Administrator token. A random per-process one replaces it.
    bundled = "sentinel-vision-dev-secret-change-in-production"
    s = Settings(demo_mode=True, jwt_secret=bundled)
    assert s.demo_mode is True
    assert s.jwt_secret != bundled
    assert len(s.jwt_secret) >= 32
    assert Settings(demo_mode=True, jwt_secret=bundled).jwt_secret != s.jwt_secret


def test_demo_mode_keeps_a_real_configured_secret():
    real_secret = "x" * 40
    assert Settings(demo_mode=True, jwt_secret=real_secret).jwt_secret == real_secret
