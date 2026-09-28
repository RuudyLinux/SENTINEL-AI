"""GET /api/cameras/nearby — the GIS query behind "which other cameras could
have seen this". Runs the haversine path here (SQLite); the PostGIS path is
exercised against a real PostGIS server by tools/postgres_verify.py --postgis."""
import pytest

from app import geo, models
from conftest import delete_cameras_by_code

# Ahmedabad junctions, real coordinates; distances below from the haversine.
PALDI = (23.0120, 72.5627)
NEAR = (23.0160, 72.5627)     # ~445 m north
FAR = (23.0400, 72.5627)      # ~3.1 km north
CODES = ["NB-PALDI", "NB-NEAR", "NB-FAR", "NB-UNKNOWN", "NB-RETIRED"]


@pytest.fixture
def cameras(db_session):
    delete_cameras_by_code(db_session, CODES)
    rows = {}
    for code, (lat, lng), retired in (
        ("NB-PALDI", PALDI, False), ("NB-NEAR", NEAR, False), ("NB-FAR", FAR, False),
        ("NB-UNKNOWN", (0.0, 0.0), False), ("NB-RETIRED", NEAR, True),
    ):
        cam = models.Camera(camera_code=code, name=code, source_type="mock_vms", source_uri="",
                            lat=lat, lng=lng, retired=retired)
        db_session.add(cam)
        rows[code] = cam
    db_session.commit()
    yield rows
    delete_cameras_by_code(db_session, CODES)


def _get(client, token, **params):
    return client.get("/api/cameras/nearby", params=params, headers={"Authorization": f"Bearer {token}"})


def test_returns_cameras_in_radius_nearest_first(client, admin_token, cameras):
    r = _get(client, admin_token, lat=PALDI[0], lng=PALDI[1], radius_m=1000)
    assert r.status_code == 200, r.text
    got = [(c["camera_code"], c["distance_m"]) for c in r.json() if c["camera_code"].startswith("NB-")]
    assert [code for code, _ in got] == ["NB-PALDI", "NB-NEAR"]
    assert got[0][1] == 0
    assert 430 < got[1][1] < 460


def test_wider_radius_reaches_the_far_camera(client, admin_token, cameras):
    r = _get(client, admin_token, lat=PALDI[0], lng=PALDI[1], radius_m=5000)
    codes = [c["camera_code"] for c in r.json() if c["camera_code"].startswith("NB-")]
    assert codes == ["NB-PALDI", "NB-NEAR", "NB-FAR"]


def test_unknown_positions_and_retired_cameras_are_never_returned(client, admin_token, cameras):
    r = _get(client, admin_token, lat=PALDI[0], lng=PALDI[1], radius_m=50_000)
    codes = {c["camera_code"] for c in r.json()}
    assert "NB-UNKNOWN" not in codes
    assert "NB-RETIRED" not in codes


def test_exclude_id_leaves_the_source_camera_out(client, admin_token, cameras):
    r = _get(client, admin_token, lat=PALDI[0], lng=PALDI[1], radius_m=1000, exclude_id=cameras["NB-PALDI"].id)
    assert [c["camera_code"] for c in r.json() if c["camera_code"].startswith("NB-")] == ["NB-NEAR"]


def test_zero_zero_is_refused_as_a_query_point(client, admin_token):
    assert _get(client, admin_token, lat=0, lng=0).status_code == 400


def test_out_of_range_input_is_rejected(client, admin_token):
    assert _get(client, admin_token, lat=91, lng=72).status_code == 422
    assert _get(client, admin_token, lat=23, lng=72, radius_m=0).status_code == 422
    assert _get(client, admin_token, lat=23, lng=72, radius_m=100_000).status_code == 422


def test_requires_authentication(client):
    assert client.get("/api/cameras/nearby", params={"lat": 23, "lng": 72}).status_code == 401


def test_haversine_matches_a_known_distance():
    # One degree of latitude is ~111.2 km on the mean-radius sphere.
    assert geo.haversine_m(23.0, 72.0, 24.0, 72.0) == pytest.approx(111_195, rel=1e-3)


def test_sqlite_uses_the_fallback(db_session):
    assert geo.postgis_ready(db_session) is False
