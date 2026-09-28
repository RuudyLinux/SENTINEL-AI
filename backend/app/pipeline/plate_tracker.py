"""Per-track plate identity: votes across frames, and when to run OCR.

Each ByteTrack track accumulates its plate reads, so one bad frame can't
overwrite a good result and a stationary car updates one Plate row instead of
adding one per cycle. Reads vote by summed confidence; the reported confidence
is the winner's peak:

    GJ05AB1234 @ 0.72, 0.91, 0.94, 0.89  ->  GJ05AB1234 @ 0.94, 4 reads

In memory and per process: a cache in front of the Plate/Vehicle rows, so a
restart only costs re-reading.
"""
import time
from dataclasses import dataclass, field

from ..config import settings


@dataclass
class _Vote:
    summed_confidence: float = 0.0
    peak_confidence: float = 0.0
    reads: int = 0
    # latest raw OCR string that normalized to this text, for plate_text_raw
    raw: str = ""
    # provenance of the latest read of this text; never folded into confidence
    variant: str = ""
    variants_agreeing: int = 1


@dataclass(frozen=True)
class Consensus:
    """Temporal evidence for one track's plate, see TrackPlateState.consensus."""
    text: str
    peak_confidence: float
    observations: int
    total_observations: int
    agreement: float
    competing_text: "str | None" = None
    competing_observations: int = 0

    @property
    def is_corroborated(self) -> bool:
        # count of agreeing frames only, confidence is weighed separately
        return self.observations >= settings.plate_min_observations


@dataclass
class TrackPlateState:
    """Accumulated plate evidence for one tracked vehicle on one camera."""
    camera_id: str
    track_id: str
    votes: dict[str, _Vote] = field(default_factory=dict)
    first_seen_mono: float = field(default_factory=time.monotonic)
    last_seen_mono: float = field(default_factory=time.monotonic)
    last_ocr_mono: float = 0.0
    last_persist_mono: float = 0.0
    # last plate location, full-frame pixels. None while OCR falls back to
    # the whole vehicle crop
    last_plate_bbox: list[float] | None = None
    # last crop OCR read, kept until the sighting's evidence is saved. a few
    # KB per live track, pruned with the rest
    last_plate_crop: object | None = None
    # once set, later frames update this Plate row instead of inserting
    plate_row_id: str | None = None
    vehicle_id: str | None = None
    # models.Track row, written for every tracked vehicle, read plate or not
    track_row_id: str | None = None
    last_track_persist_mono: float = 0.0
    # text the Plate row was last written with; if the winner flips the row
    # needs rewriting
    persisted_text: str | None = None

    def best(self) -> "tuple[str, float, int] | None":
        """(plate_text, peak_confidence, total_reads_for_that_text) or None."""
        if not self.votes:
            return None
        text = max(self.votes, key=lambda t: self.votes[t].summed_confidence)
        vote = self.votes[text]
        return text, vote.peak_confidence, vote.reads

    def consensus(self) -> "Consensus | None":
        """Winning text and the signals behind it, or None if nothing was read.

        peak_confidence is the best OCR confidence seen for the text;
        observations / total_observations count agreeing vs all passing reads
        (agreement is their ratio); competing_* is the runner-up. The signals
        are reported separately, not combined into one score.
        """
        best = self.best()
        if best is None:
            return None
        text, peak, reads = best
        total = self.total_reads()
        competing = sorted(
            ((other, vote.reads) for other, vote in self.votes.items() if other != text),
            key=lambda item: item[1], reverse=True,
        )
        return Consensus(
            text=text,
            peak_confidence=peak,
            observations=reads,
            total_observations=total,
            agreement=(reads / total) if total else 0.0,
            competing_text=competing[0][0] if competing else None,
            competing_observations=competing[0][1] if competing else 0,
        )

    def total_reads(self) -> int:
        return sum(v.reads for v in self.votes.values())

    def is_stable(self) -> bool:
        """Enough agreeing reads and a high enough peak to stop re-reading every
        cycle."""
        best = self.best()
        if best is None:
            return False
        _, peak, reads = best
        return reads >= settings.plate_min_reads_for_stability and peak >= settings.plate_stable_confidence


# (camera_id, track_id) -> state
_TRACKS: dict[tuple[str, str], TrackPlateState] = {}


def _key(camera_id: str, track_id: str) -> tuple[str, str]:
    return (str(camera_id), str(track_id))


def _prune(now_mono: float) -> None:
    # ByteTrack never says a track is gone, the TTL is what bounds this dict
    ttl = settings.plate_track_ttl_seconds
    stale = [k for k, s in _TRACKS.items() if now_mono - s.last_seen_mono > ttl]
    for k in stale:
        _TRACKS.pop(k, None)


def touch(camera_id: str, track_id: str) -> TrackPlateState:
    now = time.monotonic()
    _prune(now)
    key = _key(camera_id, track_id)
    state = _TRACKS.get(key)
    if state is None:
        state = TrackPlateState(camera_id=str(camera_id), track_id=str(track_id))
        _TRACKS[key] = state
    state.last_seen_mono = now
    return state


def should_ocr(camera_id: str, track_id: str) -> bool:
    """Whether to spend an OCR pass on this track this frame. Unsettled tracks
    are read every cycle, stable ones every plate_reverify_seconds."""
    state = _TRACKS.get(_key(camera_id, track_id))
    if state is None or not state.is_stable():
        return True
    return (time.monotonic() - state.last_ocr_mono) >= settings.plate_reverify_seconds


def record_read(
    camera_id: str,
    track_id: str,
    plate_text: str,
    confidence: float,
    raw_text: str = "",
    plate_bbox: list[float] | None = None,
    variant: str = "",
    variants_agreeing: int = 1,
    plate_crop=None,
) -> TrackPlateState:
    """Add one gate-passing read to the track's votes. Only gate-passing reads
    belong here. variant provenance doesn't weight the vote.
    """
    state = touch(camera_id, track_id)
    state.last_ocr_mono = time.monotonic()
    if plate_bbox is not None:
        state.last_plate_bbox = plate_bbox
    if plate_crop is not None:
        state.last_plate_crop = plate_crop
    vote = state.votes.setdefault(plate_text, _Vote())
    vote.summed_confidence += confidence
    vote.peak_confidence = max(vote.peak_confidence, confidence)
    vote.reads += 1
    if raw_text:
        vote.raw = raw_text
    if variant:
        vote.variant = variant
    vote.variants_agreeing = variants_agreeing
    return state


def consensus(camera_id: str, track_id: str) -> "Consensus | None":
    state = _TRACKS.get(_key(camera_id, track_id))
    return state.consensus() if state is not None else None


def has_consensus(camera_id: str, track_id: str) -> bool:
    """Whether the plate has enough agreeing frames to be a trusted sighting.

    Without consensus the sighting is still persisted but marked
    pending_review, since a car crossing in one cycle only gets one read.
    plate_require_consensus makes this a hard gate; plate_min_observations=1
    disables it.
    """
    result = consensus(camera_id, track_id)
    return result is not None and result.is_corroborated


def should_persist(camera_id: str, track_id: str, new_read: bool) -> bool:
    """Whether the sighting row needs a write this frame: no row yet, a new read,
    a changed vote winner, or the refresh interval elapsed (keeps last_seen
    current for a parked car).
    """
    state = _TRACKS.get(_key(camera_id, track_id))
    if state is None:
        return False
    if state.plate_row_id is None:
        return True
    if new_read:
        return True
    best = state.best()
    if best is not None and best[0] != state.persisted_text:
        return True
    return (time.monotonic() - state.last_persist_mono) >= settings.plate_sighting_refresh_seconds


def mark_ocr_attempt(camera_id: str, track_id: str) -> None:
    """Record an OCR attempt that produced nothing usable, so an unreadable track
    is re-read only at the reverify interval."""
    state = touch(camera_id, track_id)
    state.last_ocr_mono = time.monotonic()


def should_persist_track(camera_id: str, track_id: str) -> bool:
    # same throttle as should_persist: first sight, then on the refresh interval
    state = _TRACKS.get(_key(camera_id, track_id))
    if state is None:
        return False
    if state.track_row_id is None:
        return True
    return (time.monotonic() - state.last_track_persist_mono) >= settings.plate_sighting_refresh_seconds


def bind_track_row(camera_id: str, track_id: str, track_row_id: str) -> None:
    state = _TRACKS.get(_key(camera_id, track_id))
    if state is not None:
        state.track_row_id = track_row_id
        state.last_track_persist_mono = time.monotonic()


def get(camera_id: str, track_id: str) -> TrackPlateState | None:
    return _TRACKS.get(_key(camera_id, track_id))


def bind_plate_row(
    camera_id: str, track_id: str, plate_row_id: str, vehicle_id: str, plate_text: str,
) -> None:
    # later frames update this row instead of inserting another sighting
    state = _TRACKS.get(_key(camera_id, track_id))
    if state is not None:
        state.plate_row_id = plate_row_id
        state.vehicle_id = vehicle_id
        state.persisted_text = plate_text
        state.last_persist_mono = time.monotonic()


def release_camera(camera_id: str) -> None:
    # called from worker.stop_worker next to detector.release_model
    for key in [k for k in _TRACKS if k[0] == str(camera_id)]:
        _TRACKS.pop(key, None)


def reset() -> None:
    # for tests, module state is process-global
    _TRACKS.clear()
