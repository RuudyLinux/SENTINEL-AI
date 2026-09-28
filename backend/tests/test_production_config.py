"""DEMO_MODE=false refuses an insecure or default JWT secret at startup;
demo mode still starts without one."""
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
    # demo mode starts without JWT_SECRET, but the bundled secret is public,
    # so a random per-process one is used instead
    bundled = "sentinel-vision-dev-secret-change-in-production"
    s = Settings(demo_mode=True, jwt_secret=bundled)
    assert s.demo_mode is True
    assert s.jwt_secret != bundled
    assert len(s.jwt_secret) >= 32
    assert Settings(demo_mode=True, jwt_secret=bundled).jwt_secret != s.jwt_secret


def test_demo_mode_keeps_a_real_configured_secret():
    real_secret = "x" * 40
    assert Settings(demo_mode=True, jwt_secret=real_secret).jwt_secret == real_secret
