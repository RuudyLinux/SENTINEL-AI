"""An ID whose object changes kind is split (pipeline/detector._published_track_id).

ByteTrack matches by position only; on real night footage a lost car's ID was
taken over by a motorbike rider. One ID spanning two objects would mix their
plate votes and loitering time.
"""
import pytest

from app.pipeline import detector


@pytest.fixture(autouse=True)
def _fresh_camera():
    detector.release_model("cam-k")
    yield
    detector.release_model("cam-k")


def test_same_kind_keeps_the_tracker_id():
    assert detector._published_track_id("cam-k", 7, "car") == 7
    assert detector._published_track_id("cam-k", 7, "truck") == 7  # car/truck flips are one vehicle


def test_rider_and_bike_trading_an_id_stay_one_object():
    assert detector._published_track_id("cam-k", 3, "motorbike") == 3
    assert detector._published_track_id("cam-k", 3, "person") == 3


def test_car_id_taken_over_by_a_rider_becomes_a_new_id_and_stays_new():
    assert detector._published_track_id("cam-k", 4, "car") == 4
    split = detector._published_track_id("cam-k", 4, "motorbike")
    assert split != 4
    assert detector._published_track_id("cam-k", 4, "person") == split  # the rider keeps its new ID
    assert detector._published_track_id("cam-k", 4, "car") not in (4, split)


def test_release_forgets_the_camera_ids():
    detector._published_track_id("cam-k", 4, "car")
    detector._published_track_id("cam-k", 4, "motorbike")
    detector.release_model("cam-k")
    assert detector._published_track_id("cam-k", 4, "motorbike") == 4
