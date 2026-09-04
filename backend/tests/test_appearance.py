"""Person appearance-similarity signature (Phase 5). Explicitly testing visual
similarity behavior only — nothing here claims or tests identity."""
import numpy as np
import pytest

from app.pipeline.appearance import compute_signature, similarity


def _solid_crop(color_bgr, size=32):
    crop = np.zeros((size, size, 3), dtype=np.uint8)
    crop[:, :] = color_bgr
    return crop


def test_identical_crops_score_near_one():
    crop = _solid_crop((30, 60, 200))  # a reddish color in BGR
    sig_a = compute_signature(crop)
    sig_b = compute_signature(crop.copy())
    assert sig_a is not None and sig_b is not None
    assert similarity(sig_a, sig_b) > 0.99


def test_very_different_colors_score_low():
    red_sig = compute_signature(_solid_crop((0, 0, 255)))
    blue_sig = compute_signature(_solid_crop((255, 0, 0)))
    assert similarity(red_sig, blue_sig) < 0.5


def test_too_small_crop_returns_none_not_fabricated():
    tiny = np.zeros((3, 3, 3), dtype=np.uint8)
    assert compute_signature(tiny) is None


def test_similarity_handles_missing_signatures_safely():
    sig = compute_signature(_solid_crop((10, 20, 30)))
    assert similarity(None, sig) == 0.0
    assert similarity(sig, None) == 0.0
    assert similarity(None, None) == 0.0


def test_find_similar_person_detections_skips_unsigned_and_respects_threshold(db_session):
    from app import models
    from app.pipeline.correlate import find_similar_person_detections

    cam_a = models.Camera(camera_code="C-901", name="Cam A", source_type="mock_vms", source_uri="")
    cam_b = models.Camera(camera_code="C-902", name="Cam B", source_type="mock_vms", source_uri="")
    db_session.add_all([cam_a, cam_b])
    db_session.flush()

    red_sig = compute_signature(_solid_crop((0, 0, 255)))
    blue_sig = compute_signature(_solid_crop((255, 0, 0)))

    reference = models.Detection(camera_id=cam_a.id, cls="person", confidence=0.9, bbox=[0, 0, 10, 10], appearance_signature=red_sig)
    similar_other_cam = models.Detection(camera_id=cam_b.id, cls="person", confidence=0.9, bbox=[0, 0, 10, 10], appearance_signature=red_sig)
    dissimilar = models.Detection(camera_id=cam_b.id, cls="person", confidence=0.9, bbox=[0, 0, 10, 10], appearance_signature=blue_sig)
    unsigned = models.Detection(camera_id=cam_b.id, cls="person", confidence=0.9, bbox=[0, 0, 10, 10], appearance_signature=None)
    db_session.add_all([reference, similar_other_cam, dissimilar, unsigned])
    db_session.commit()

    results = find_similar_person_detections(db_session, reference.id, min_similarity=0.6)
    result_ids = {r["detection_id"] for r in results}
    assert similar_other_cam.id in result_ids
    assert dissimilar.id not in result_ids
    assert unsigned.id not in result_ids
