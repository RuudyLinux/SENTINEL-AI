"""No route is reachable without a credential.

POST /api/cameras/test-connection had no auth at all, the only camera route
like that. It opens a URI you give it, so it was an unauthenticated SSRF
probe, and it held a worker thread for the whole open timeout per request.

Walks the real route table instead of naming endpoints, so a new open route
fails here right away.
"""

import pytest

from app.main import app

# The only routes legitimately reachable without a bearer token, each with the
# reason it is exempt. Anything else must carry an auth dependency.
PUBLIC_ROUTES = {
    ("POST", "/api/auth/login"): "issues the token; cannot itself require one",
    ("GET", "/api/health"): "liveness probe, exposes no data beyond 'the service is up'",
    # <img src>/<a href> can't send a header, so these take a short-lived
    # signed resource token (get_user_from_resource_token); not open
    ("GET", "/api/evidence/{evidence_id}/file"): "signed resource token",
    ("GET", "/api/evidence/incidents/{incident_id}/package"): "signed resource token",
    ("GET", "/api/streams/{camera_id}/mjpeg"): "signed resource token",
    ("GET", "/api/streams/{camera_id}/snapshot.jpg"): "signed resource token",
    # Validates a scrape token or an Administrator JWT in its own handler,
    # because a Prometheus scraper cannot perform a JWT login.
    ("GET", "/api/metrics"): "scrape token or Administrator JWT, checked in-handler",
}

_AUTH_MARKERS = ("get_current_user", "require_roles", "get_user_from_resource_token", "get_user_from_token")


def _routes():
    for route in app.routes:
        methods = getattr(route, "methods", None) or set()
        path = getattr(route, "path", "")
        endpoint = getattr(route, "endpoint", None)
        if not path.startswith("/api") or endpoint is None:
            continue
        for method in methods:
            if method in ("HEAD", "OPTIONS"):
                continue
            yield method, path, endpoint


def test_every_api_route_requires_a_credential():
    import inspect

    unprotected = []
    for method, path, endpoint in _routes():
        if (method, path) in PUBLIC_ROUTES:
            continue
        try:
            source = inspect.getsource(endpoint)
        except (OSError, TypeError):  # pragma: no cover - defensive
            continue
        signature = source.split("):")[0] if "):" in source else source
        if not any(marker in signature for marker in _AUTH_MARKERS):
            unprotected.append(f"{method} {path} -> {endpoint.__name__}")

    assert not unprotected, (
        "these routes accept requests with no credential; add an auth dependency, "
        "or add them to PUBLIC_ROUTES with a stated reason:\n  " + "\n  ".join(unprotected)
    )


def test_test_connection_rejects_an_anonymous_caller(client):
    """The specific regression: an anonymous source probe must be refused."""
    resp = client.post(
        "/api/cameras/test-connection",
        data={"source_type": "rtsp", "source_uri": "rtsp://127.0.0.1:9/probe"},
    )
    assert resp.status_code == 401


def test_test_connection_rejects_a_role_that_may_not_manage_cameras(client, db_session):
    """Same role as camera creation, not just any logged-in user; an Auditor
    has no business making the server open connections."""
    from app import models
    from app.security import create_access_token, hash_password

    role = db_session.query(models.Role).filter(models.Role.name == "Auditor").first()
    if role is None:
        role = models.Role(name="Auditor", description="read-only")
        db_session.add(role)
        db_session.flush()
    auditor = db_session.query(models.User).filter(models.User.username == "authz_auditor").first()
    if auditor is None:
        auditor = models.User(
            username="authz_auditor", password_hash=hash_password("testpass123"),
            full_name="Auditor", role_id=role.id,
        )
        db_session.add(auditor)
        db_session.commit()
        db_session.refresh(auditor)

    resp = client.post(
        "/api/cameras/test-connection",
        data={"source_type": "rtsp", "source_uri": "rtsp://127.0.0.1:9/probe"},
        headers={"Authorization": f"Bearer {create_access_token(auditor)}"},
    )
    assert resp.status_code == 403


@pytest.mark.parametrize("path", [
    "/api/vehicles",
    "/api/vehicles/by-plate/GJ05AB1234",
    "/api/vehicles/veh_x/summary",
    "/api/vehicles/veh_x/route",
    "/api/vehicles/veh_x/sightings",
    "/api/plates",
])
def test_v2_vehicle_endpoints_require_authentication(client, path):
    """The V2 plate/journey/investigation routes expose vehicle movement and
    refuse anonymous callers."""
    assert client.get(path).status_code == 401
