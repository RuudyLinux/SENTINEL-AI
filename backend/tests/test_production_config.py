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


def test_demo_mode_still_allows_the_bundled_dev_secret():
    # The whole point: this must NOT raise — demo/judge flow stays working.
    s = Settings(demo_mode=True, jwt_secret="sentinel-vision-dev-secret-change-in-production")
    assert s.demo_mode is True
