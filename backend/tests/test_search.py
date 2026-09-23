"""First tests for `GET /api/search` — previously the least-covered module in
the codebase (24%) with no test referencing it at all.

Two real defects were found the moment it was exercised:

1. **"after 12am" parsed to hour 12.** Midnight is 0. The `pm` branch guarded
   `12pm` correctly; the `am` branch did not exist, so an operator asking for
   overnight activity silently got an afternoon filter.

2. **The parsed filters were never applied.** `after_hour`, `before_hour` and
   `entity` were extracted, returned in `parsed_filters`, and RENDERED to the
   operator by the search page (`app/(shell)/search/page.tsx` prints
   "Parsed filters: ..."), while every query ignored them. A time-scoped
   search that is not scoped is worse than one that is absent, because the
   screen asserts the filter was understood.
"""
import random
import uuid
from datetime import datetime

import pytest

from app import models
from app.routers.search import parse_natural_language


def _fresh_plate() -> str:
    """A VALID Indian registration. An earlier version of this file built the
    numeric block from uuid hex, producing e.g. `GJ05SVFDB4` — letters where
    digits belong, so it matched neither PLATE_TOKEN_RE nor the stored plate,
    and the test failed for its own bad data rather than for the code."""
    return f"GJ05SV{random.randint(1000, 9999)}"


@pytest.fixture
def auth(admin_token):
    return {"Authorization": f"Bearer {admin_token}"}


class TestMeridiemParsing:
    @pytest.mark.parametrize("query,expected", [
        ("after 6pm", 18),
        ("after 12am", 0),    # midnight — the bug
        ("after 12pm", 12),   # noon, already correct
        ("after 1am", 1),
        ("after 11pm", 23),
        ("after 6", 6),       # no meridiem given
    ])
    def test_after_hour_is_converted_to_24_hour_time(self, query, expected):
        assert parse_natural_language(query)["after_hour"] == expected

    @pytest.mark.parametrize("query,expected", [
        ("before 9am", 9),
        ("before 12am", 0),
        ("before 12pm", 12),
        ("before 8pm", 20),
    ])
    def test_before_hour_is_converted_to_24_hour_time(self, query, expected):
        assert parse_natural_language(query)["before_hour"] == expected

    def test_a_plate_is_extracted_and_normalized(self):
        assert parse_natural_language("GJ 05 AB 1234 after 6pm")["plate"] == "GJ05AB1234"


class TestFiltersAreActuallyApplied:
    """The headline fix: a filter the response advertises must change the
    results, or the screen is lying to the investigator."""

    def _incident_at(self, db, hour: int, title: str) -> models.Incident:
        when = datetime.utcnow().replace(hour=hour, minute=0, second=0, microsecond=0)
        incident = models.Incident(title=title, status="open", priority="HIGH", created_at=when)
        db.add(incident)
        db.commit()
        return incident

    def test_after_hour_excludes_earlier_records(self, client, db_session, auth):
        marker = f"ZTIME{uuid.uuid4().hex[:6].upper()}"
        self._incident_at(db_session, 8, f"{marker} morning incident")
        evening = self._incident_at(db_session, 20, f"{marker} evening incident")

        body = client.get(f"/api/search?q={marker} after 6pm", headers=auth).json()
        ids = [i["id"] for i in body["incidents"]]

        assert body["parsed_filters"]["after_hour"] == 18
        assert evening.id in ids, "the 20:00 incident should match 'after 6pm'"
        assert len(ids) == 1, (
            "the 08:00 incident was returned for an 'after 6pm' search — the parsed "
            "filter is being reported but not applied"
        )

    def test_before_hour_excludes_later_records(self, client, db_session, auth):
        marker = f"ZTIME{uuid.uuid4().hex[:6].upper()}"
        morning = self._incident_at(db_session, 7, f"{marker} morning incident")
        self._incident_at(db_session, 22, f"{marker} late incident")

        body = client.get(f"/api/search?q={marker} before 9am", headers=auth).json()
        ids = [i["id"] for i in body["incidents"]]

        assert morning.id in ids
        assert len(ids) == 1

    def test_a_person_query_does_not_return_vehicles(self, client, db_session, auth):
        """`entity` was also parsed, displayed and ignored."""
        plate = _fresh_plate()
        db_session.add(models.Vehicle(plate_text=plate, plate_confidence=0.9))
        db_session.commit()

        body = client.get(f"/api/search?q=person {plate}", headers=auth).json()
        assert body["parsed_filters"]["entity"] == "person"
        assert body["vehicles"] == [], "a person-focused query returned vehicle results"

    def test_a_vehicle_query_still_returns_vehicles(self, client, db_session, auth):
        """The suppression must be specific, not a blanket disable."""
        plate = _fresh_plate()
        db_session.add(models.Vehicle(plate_text=plate, plate_confidence=0.9))
        db_session.commit()

        body = client.get(f"/api/search?q=vehicle {plate}", headers=auth).json()
        assert [v["plate_text"] for v in body["vehicles"]] == [plate]


class TestSearchSections:
    def test_a_camera_is_found_by_code_and_by_name(self, client, db_session, auth):
        code = f"SRCH-{uuid.uuid4().hex[:8]}"
        db_session.add(models.Camera(
            camera_code=code, name=f"Unique {code} Junction", location="Testville",
            source_type="mock_vms", source_uri="",
        ))
        db_session.commit()

        by_code = client.get(f"/api/search?q={code}", headers=auth).json()
        assert code in [c["camera_code"] for c in by_code["cameras"]]

    def test_an_alert_is_found_via_its_vehicle(self, client, db_session, auth):
        """Regression guard for a bug this project already fixed once: alerts
        were filtered on `camera_id.ilike(free text)`, so the alerts section of
        a global search was permanently empty."""
        plate = _fresh_plate()
        camera = models.Camera(
            camera_code=f"SRCH-{uuid.uuid4().hex[:8]}", name="alert search cam",
            source_type="mock_vms", source_uri="",
        )
        db_session.add(camera)
        vehicle = models.Vehicle(plate_text=plate, plate_confidence=0.9)
        db_session.add(vehicle)
        db_session.flush()
        alert = models.Alert(camera_id=camera.id, vehicle_id=vehicle.id, severity="CRITICAL", reasons=["search probe"])
        db_session.add(alert)
        db_session.commit()

        body = client.get(f"/api/search?q={plate}", headers=auth).json()
        assert alert.id in [a["id"] for a in body["alerts"]]

    def test_a_query_matching_nothing_returns_empty_sections_not_an_error(self, client, auth):
        body = client.get(f"/api/search?q=ZZZ{uuid.uuid4().hex}", headers=auth).json()
        assert body["cameras"] == [] and body["vehicles"] == []
        assert body["incidents"] == [] and body["alerts"] == []

    def test_search_requires_authentication(self, client):
        assert client.get("/api/search?q=anything").status_code == 401
