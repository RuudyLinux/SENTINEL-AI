"""Per-track plate identity: temporal aggregation, confidence voting, OCR gating.

Fixes three related problems in the original pipeline at once:

1. **No vehicle-track <-> plate association.** `Detection.track_id` was
   persisted, but nothing carried it into the ANPR path, so ByteTrack's track
   284 and the plate GJ05AB1234 read off it were never tied together. Every
   passing OCR frame was an independent event.

2. **No temporal aggregation.** One bad OCR frame could overwrite a reliable
   result, because there was no "result" — only the latest read.

3. **Unbounded Plate row growth.** A vehicle stopped at a signal produced one
   Plate row per inference cycle for as long as it sat there. Since
   `correlate.get_route()` builds a vehicle's cross-camera journey FROM Plate
   rows, that turned a single sighting into dozens of identical route hops.

The model is one accumulator per `(camera_id, track_id)`. Reads vote; the
winner is decided by summed confidence (so four consistent mid-confidence reads
beat one lucky high-confidence outlier), and the *reported* confidence is the
winner's PEAK observed confidence, which is the honest answer to "how well did
we ever actually read this plate":

    GJ05AB1234 @ 0.72, 0.91, 0.94, 0.89  ->  GJ05AB1234 @ 0.94, 4 reads

State is process-local and in-memory, keyed and pruned exactly like
`rules_engine._zone_presence` / `_last_alert_at` — the same convention already
used for per-track state in this codebase. It is a cache, never a system of
record: the durable answer is the Plate/Vehicle row it produces, so losing this
on restart costs re-OCR, not data.
"""
import time
from dataclasses import dataclass, field

from ..config import settings


@dataclass
class _Vote:
    summed_confidence: float = 0.0
    peak_confidence: float = 0.0
    reads: int = 0
    # The most recent raw (un-normalized) OCR string that normalized to this
    # text. Kept for the audit trail — Plate.plate_text_raw records what OCR
    # literally returned, not only what we cleaned it up to.
    raw: str = ""
    # Which preprocessing variant produced the most recent read of this text,
    # and how many variants agreed on it. Provenance for the audit trail: the
    # two are stored separately from confidence and are never folded into it.
    variant: str = ""
    variants_agreeing: int = 1


@dataclass(frozen=True)
class Consensus:
    """Temporal evidence for one tracked vehicle's plate. See
    `TrackPlateState.consensus` for what each field means and why they are not
    combined into a single number."""
    text: str
    peak_confidence: float
    observations: int
    total_observations: int
    agreement: float
    competing_text: "str | None" = None
    competing_observations: int = 0

    @property
    def is_corroborated(self) -> bool:
        """Whether enough independent frames agreed to treat this as settled.

        Purely a count of agreeing observations against the configured
        threshold — it does not consult confidence, because confidence is a
        separate signal the caller weighs separately.
        """
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
    # Where the plate was last localized, in FULL-FRAME pixel coordinates.
    # None while OCR is still falling back to the whole vehicle crop.
    last_plate_bbox: list[float] | None = None
    # The plate crop image OCR last actually read, held only until the sighting
    # row is written and its evidence saved. In-memory and per-track, bounded by
    # the same TTL prune as the rest of this state — a plate crop is a few KB,
    # and one per live track is the same order as the frame buffers the worker
    # already holds.
    last_plate_crop: object | None = None
    # Set once this track's Plate row exists, so subsequent frames UPDATE that
    # row instead of inserting another one (problem 3 above).
    plate_row_id: str | None = None
    vehicle_id: str | None = None
    # The models.Track row for this ByteTrack id. Written for every tracked
    # vehicle, whether or not its plate is ever read — a vehicle we can follow
    # but cannot identify is still real, trackable intelligence.
    track_row_id: str | None = None
    last_track_persist_mono: float = 0.0
    # Text this track's Plate row was last written with — a change means the
    # vote winner flipped and the row must be rewritten even if nothing else
    # would have triggered a persist.
    persisted_text: str | None = None

    def best(self) -> "tuple[str, float, int] | None":
        """(plate_text, peak_confidence, total_reads_for_that_text) or None."""
        if not self.votes:
            return None
        text = max(self.votes, key=lambda t: self.votes[t].summed_confidence)
        vote = self.votes[text]
        return text, vote.peak_confidence, vote.reads

    def consensus(self) -> "Consensus | None":
        """The full temporal picture for this track, or None if nothing was read.

        Reports the winning text alongside every signal that decided it, kept
        separate rather than collapsed into one score:

        - `peak_confidence` — the best the OCR engine ever actually reported for
          this text. The honest answer to "how well did we ever read this".
        - `observations` — how many gate-passing reads agreed on it.
        - `total_observations` — how many gate-passing reads there were in all.
        - `agreement` — observations / total_observations, i.e. how UNANIMOUS the
          track's reads were. A plate read 4 times with 4 agreeing is stronger
          evidence than one read 4 times with 2 agreeing and 2 saying something
          else, even at identical confidence.
        - `competing` — the runner-up text, if any, so an operator can see what
          the track was confused between rather than only the winner.

        Nothing here is multiplied into a fabricated probability. The gate
        (`has_consensus`) reasons over the fields explicitly.
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
        """True once the winning read has enough corroboration to stop
        re-OCRing this track every cycle. Requires both a minimum number of
        agreeing reads AND a peak confidence above the stability floor — a
        plate read four times at 0.36 each is consistent but not trustworthy,
        and should keep being re-checked while the vehicle is still visible."""
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
    """Drop tracks not seen for longer than the TTL. ByteTrack retires a track
    id when the object leaves frame and never tells us, so this is the only
    thing bounding the dict on a camera that has been running for days."""
    ttl = settings.plate_track_ttl_seconds
    stale = [k for k, s in _TRACKS.items() if now_mono - s.last_seen_mono > ttl]
    for k in stale:
        _TRACKS.pop(k, None)


def touch(camera_id: str, track_id: str) -> TrackPlateState:
    """Mark a track as seen this frame, creating its accumulator if new."""
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
    """Whether to spend an OCR pass on this track this frame.

    This is the pipeline's main CPU saving. Previously every vehicle detection
    on every inference cycle ran a full EasyOCR pass — by far the most
    expensive operation in the loop. Now:

    - a track with no confident plate yet is read every cycle (nothing to lose);
    - a track with a stable plate is re-verified only every
      `plate_reverify_seconds`, which catches a genuine mid-track correction
      without paying for OCR on a car we have already read four times.
    """
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
    """Add one gate-passing OCR read to this track's vote tally.

    Only reads that already cleared the quality gate should reach here — voting
    must not be polluted by reads the gate rejected, or a persistent misread of
    a bumper sticker would out-vote the real plate.

    `variant`/`variants_agreeing` are recorded as provenance for the audit
    trail. They do NOT influence the vote weight: the vote is decided by summed
    OCR confidence across frames, and letting cross-variant agreement also
    inflate a frame's weight would count the same corroboration twice.
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
    """The temporal consensus for one track, or None if it has no reads."""
    state = _TRACKS.get(_key(camera_id, track_id))
    return state.consensus() if state is not None else None


def has_consensus(camera_id: str, track_id: str) -> bool:
    """Whether this track's plate has enough temporal evidence to be persisted
    as a trusted sighting.

    The behavior change this exists for: previously the FIRST gate-passing OCR
    read created a Vehicle and a Plate row. One lucky frame — one
    plate-shaped-but-wrong read clearing the confidence floor — became a durable
    vehicle identity, and the benchmark says that class of read is real and
    common (plate-shaped-but-wrong reads outnumber correct ones on the labelled
    corpus). Requiring corroboration across frames is the defence the temporal
    layer was built to provide, and it was not being used to gate persistence.

    What "not enough evidence" does NOT mean here is "throw the read away". A
    vehicle that crosses the frame inside a single inference cycle gets exactly
    one read and will never get another; dropping it would lose a real sighting
    to protect against a hypothetical one. So an uncorroborated read is still
    persisted — as an UNTRUSTED observation, flagged `pending_review` by
    `review_status_for` regardless of how confident that single read was, so it
    reaches an operator instead of being presented as settled intelligence.

    `settings.plate_require_consensus` turns this into a hard gate for
    deployments that would rather lose the fast-vehicle sighting than hold an
    uncorroborated one; it is off by default because silently discarding real
    observations is the worse failure for an investigative system.

    Setting `plate_min_observations = 1` restores the previous behavior
    outright, so the change is revertible in one env var.
    """
    result = consensus(camera_id, track_id)
    return result is not None and result.is_corroborated


def should_persist(camera_id: str, track_id: str, new_read: bool) -> bool:
    """Whether this track's Plate sighting row needs a write this frame.

    Without this the sighting row would be re-written on every single frame for
    as long as a vehicle stays in view — the exact per-frame write pressure the
    rest of this pipeline is carefully tuned to avoid (see worker.py's
    heartbeat throttle and db.py's WAL notes). A write happens when there is
    something real to record:

    - the row does not exist yet (first confident read of this vehicle here);
    - a new OCR read just landed (confidence/vote count actually changed);
    - the vote winner changed since the row was written;
    - the refresh interval elapsed, so `last_seen` advances and the sighting's
      dwell time stays truthful for a vehicle sitting in frame.
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
    """Record that OCR ran for this track but produced nothing usable.

    Without this, a track whose plate is genuinely unreadable (rear of a truck,
    plate out of frame, heavy motion blur) would be re-OCR'd on every single
    inference cycle forever, since `should_ocr` keys off stability and an
    unreadable track never becomes stable. The timestamp alone does not make it
    stable — it just lets the reverify interval throttle the retries."""
    state = touch(camera_id, track_id)
    state.last_ocr_mono = time.monotonic()


def should_persist_track(camera_id: str, track_id: str) -> bool:
    """Whether the models.Track row for this ByteTrack id needs a write.

    Same throttle reasoning as `should_persist`: written on first sight, then
    refreshed on an interval so `last_seen`/`detection_count` stay meaningful
    without one write per frame per tracked object.
    """
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
    """Remember which Plate/Vehicle row this track already owns, so later
    frames update it rather than inserting a duplicate sighting."""
    state = _TRACKS.get(_key(camera_id, track_id))
    if state is not None:
        state.plate_row_id = plate_row_id
        state.vehicle_id = vehicle_id
        state.persisted_text = plate_text
        state.last_persist_mono = time.monotonic()


def release_camera(camera_id: str) -> None:
    """Drop every track for a camera whose worker is stopping — mirrors
    `detector.release_model` / `clips.release_camera`, called from the same
    place (`worker.stop_worker`)."""
    for key in [k for k in _TRACKS if k[0] == str(camera_id)]:
        _TRACKS.pop(key, None)


def reset() -> None:
    """Test-isolation hook — module state is process-global by design."""
    _TRACKS.clear()
