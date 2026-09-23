"""Human-in-the-loop ANPR review (10/10 roadmap P7): low-confidence Plate
sightings are flagged pending_review rather than silently treated as
settled, and the review-queue endpoints accept/correct/reject them."""
import uuid

import pytest

from app import models
from app.config import settings
from app.pipeline.anpr import review_status_for


@pytest.fixture
def auth(admin_token):
    return {"Authorization": f"Bearer {admin_token}"}


def _make_plate(db, confidence: float, review_status: str | None = None) -> models.Plate:
    camera = models.Camera(camera_code=f"REV-{uuid.uuid4().hex[:8]}", name="review cam", source_type="video_file", source_uri="x.mp4")
    db.add(camera)
    db.flush()
    plate = models.Plate(
        camera_id=camera.id, plate_text_raw="GJ05AB1234", plate_text_normalized="GJ05AB1234",
        confidence=confidence, review_status=review_status or review_status_for(confidence),
    )
    db.add(plate)
    db.commit()
    db.refresh(plate)
    return plate


class TestReviewStatusFor:
    def test_below_floor_is_pending_review(self):
        assert review_status_for(settings.plate_review_confidence_floor - 0.01) == "pending_review"

    def test_at_or_above_floor_is_auto_accepted(self):
        assert review_status_for(settings.plate_review_confidence_floor) == "auto_accepted"

    def test_human_terminal_states_are_never_overwritten_by_a_fresh_read(self):
        assert review_status_for(0.95, current="corrected") == "corrected"
        assert review_status_for(0.95, current="rejected") == "rejected"


class TestReviewQueueApi:
    def test_low_confidence_plate_appears_in_queue(self, client, db_session, auth):
        plate = _make_plate(db_session, confidence=0.40)
        resp = client.get("/api/review/queue", headers=auth)
        assert resp.status_code == 200
        ids = [p["id"] for p in resp.json()]
        assert plate.id in ids

    def test_high_confidence_plate_does_not_appear_in_queue(self, client, db_session, auth):
        plate = _make_plate(db_session, confidence=0.95)
        resp = client.get("/api/review/queue", headers=auth)
        ids = [p["id"] for p in resp.json()]
        assert plate.id not in ids

    def test_accept_marks_auto_accepted_and_stamps_reviewer(self, client, db_session, auth):
        plate = _make_plate(db_session, confidence=0.40)
        resp = client.post(f"/api/review/{plate.id}/accept", headers=auth)
        assert resp.status_code == 200
        body = resp.json()
        assert body["review_status"] == "auto_accepted"
        assert body["reviewed_by"] is not None
        assert body["reviewed_at"] is not None

    def test_correct_stores_corrected_text_separately_from_ocr_fields(self, client, db_session, auth):
        plate = _make_plate(db_session, confidence=0.40)
        resp = client.post(f"/api/review/{plate.id}/correct", json={"corrected_text": "gj05ab1204"}, headers=auth)
        assert resp.status_code == 200
        body = resp.json()
        assert body["review_status"] == "corrected"
        assert body["corrected_text"] == "GJ05AB1204"
        # OCR fields untouched — three separate, all-preserved facts.
        assert body["plate_text_normalized"] == "GJ05AB1234"

    def test_reject_removes_it_from_the_active_queue_but_keeps_the_row(self, client, db_session, auth):
        plate = _make_plate(db_session, confidence=0.40)
        resp = client.post(f"/api/review/{plate.id}/reject", json={"reason": "bumper sticker, not a plate"}, headers=auth)
        assert resp.status_code == 200
        assert resp.json()["review_status"] == "rejected"

        queue = client.get("/api/review/queue", headers=auth).json()
        assert plate.id not in [p["id"] for p in queue]
        assert db_session.query(models.Plate).filter(models.Plate.id == plate.id).first() is not None

    def test_unauthenticated_request_is_rejected(self, client, db_session):
        plate = _make_plate(db_session, confidence=0.40)
        assert client.get("/api/review/queue").status_code == 401
        assert client.post(f"/api/review/{plate.id}/accept").status_code == 401
