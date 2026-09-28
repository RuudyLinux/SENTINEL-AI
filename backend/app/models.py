"""ORM models (doc §51 / §7)."""
import uuid
from datetime import datetime

from sqlalchemy import (
    Column, String, Float, Integer, Boolean, DateTime, ForeignKey, Index, JSON
)
from sqlalchemy import false as sa_false
from sqlalchemy.orm import relationship

from .db import Base


def uid(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:10]}"


class Role(Base):
    __tablename__ = "roles"
    id = Column(String, primary_key=True, default=lambda: uid("role"))
    name = Column(String, unique=True, nullable=False)  # Administrator, Control Room Operator, Investigator, Supervisor, Auditor
    description = Column(String, default="")
    users = relationship("User", back_populates="role")


class User(Base):
    __tablename__ = "users"
    id = Column(String, primary_key=True, default=lambda: uid("usr"))
    username = Column(String, unique=True, nullable=False)
    password_hash = Column(String, nullable=False)
    full_name = Column(String, default="")
    department = Column(String, default="")
    role_id = Column(String, ForeignKey("roles.id"))
    active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    role = relationship("Role", back_populates="users")


class Camera(Base):
    __tablename__ = "cameras"
    id = Column(String, primary_key=True, default=lambda: uid("cam"))
    camera_code = Column(String, unique=True, nullable=False)  # e.g. C-014
    name = Column(String, nullable=False)
    department = Column(String, default="Police")
    location = Column(String, default="")
    lat = Column(Float, default=0.0)
    lng = Column(Float, default=0.0)
    source_type = Column(String, nullable=False)  # webcam | video_file | rtsp (rtsp unsupported here)
    source_uri = Column(String, nullable=False)  # device index, file path, or rtsp url
    ai_person = Column(Boolean, default=True)
    ai_vehicle = Column(Boolean, default=True)
    ai_anpr = Column(Boolean, default=True)
    status = Column(String, default="offline")  # online | offline | degraded
    fps = Column(Float, default=0.0)
    resolution = Column(String, default="")
    latency_ms = Column(Float, default=0.0)
    error_count = Column(Integer, default=0)
    last_frame_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    # Catalogue linkage, only set by a catalogue sync (pipeline/catalog.py).
    # catalog_stale = no longer listed as of the last sync; kept for history.
    external_catalog_id = Column(String, nullable=True, index=True)
    catalog_codec = Column(String, default="")
    catalog_live_status = Column(String, default="")
    catalog_synced_at = Column(DateTime, nullable=True)
    catalog_stale = Column(Boolean, default=False)
    # Retired cameras are kept so their history still points at a real row.
    # Never connects, starts or counts as active again (POST /{id}/retire),
    # undo with POST /{id}/reinstate.
    retired = Column(Boolean, default=False, nullable=False, server_default=sa_false())
    # The catalogue's other two stream URLs. NULL when missing. Unlike
    # source_uri (can embed RTSP credentials, never returned by the API) these
    # are meant for clients (WHEP for browser preview, HLS as fallback), so
    # CameraOut exposes them.
    whep_url = Column(String, nullable=True)
    hls_url = Column(String, nullable=True)
    # Free-form grouping ("North Zone", "Highway Cams"), filtered client side.
    # Empty string not null so old rows migrate cleanly (ensure_columns in
    # main.py). Not `group`: reserved word, and the raw-SQL ensure_* helpers
    # don't quote it.
    camera_group = Column(String, default="")


class Detection(Base):
    __tablename__ = "detections"
    # The live camera page polls a camera's newest detections every few
    # seconds. With just the camera_id index SQLite read and sorted all of a
    # camera's rows: 27 ms on 22k and growing. With this, 50 rows, 0.12 ms.
    __table_args__ = (Index("ix_detections_camera_id_timestamp", "camera_id", "timestamp"),)
    id = Column(String, primary_key=True, default=lambda: uid("det"))
    camera_id = Column(String, ForeignKey("cameras.id"), nullable=False, index=True)
    timestamp = Column(DateTime, default=datetime.utcnow, index=True)  # PROCESSING time: when SENTINEL wrote this row
    source_timestamp = Column(DateTime, nullable=True)  # SOURCE time: when the frame was captured, if reliably known (see pipeline/timing.py)
    cls = Column(String, nullable=False)  # person | car | truck | bus | motorbike
    confidence = Column(Float, nullable=False)
    bbox = Column(JSON, default=list)  # [x1,y1,x2,y2]
    # ByteTrack id, per camera (detector.py keeps a tracker per camera).
    # Indexed, going from a sighting back to its track's frames is a core query
    track_id = Column(String, nullable=True, index=True)
    model_version = Column(String, default="")
    snapshot_path = Column(String, nullable=True)
    # HSV color-histogram signature, persons only (pipeline/appearance.py).
    # Not biometric, just visual similarity for ranking leads. Null when not
    # computed (worker.py._process_frame).
    appearance_signature = Column(JSON, nullable=True)


class Track(Base):
    """One tracked object's life on one camera. ByteTrack ids are only unique
    per predictor and detector.py keeps one per camera.

    Written by worker.py for every tracked vehicle; vehicle_id gets filled in
    once the track's plate is read, so "track 284 is GJ05AB1234" is one row
    instead of a three-join inference."""
    __tablename__ = "tracks"
    id = Column(String, primary_key=True, default=lambda: uid("trk"))
    camera_id = Column(String, ForeignKey("cameras.id"), nullable=False, index=True)
    cls = Column(String, nullable=False)
    yolo_track_id = Column(Integer, nullable=True, index=True)
    first_seen = Column(DateTime, default=datetime.utcnow)
    last_seen = Column(DateTime, default=datetime.utcnow)
    vehicle_id = Column(String, ForeignKey("vehicles.id"), nullable=True, index=True)
    # running totals so we don't recount Detection rows
    detection_count = Column(Integer, default=0)
    plate_reads = Column(Integer, default=0)


class Vehicle(Base):
    __tablename__ = "vehicles"
    id = Column(String, primary_key=True, default=lambda: uid("veh"))
    # Unique: two camera workers seeing a brand-new plate at the same moment
    # could each insert a row and split one vehicle's route/sightings/risk in
    # two, silently. Now the second insert raises IntegrityError and
    # correlate.upsert_vehicle_for_plate merges it into the winner. Multiple
    # NULLs are still allowed (tests/test_vehicle_upsert_race.py).
    #
    # Gap: SQLite's ALTER TABLE ADD COLUMN (db.ensure_columns) can't add
    # UNIQUE to an existing table, so an old SQLite DB upgraded in place only
    # gets the narrower race window until the file is recreated. Fresh
    # create_all and the Alembic/PostgreSQL path get the real constraint.
    # Noted in docs/THREAT_MODEL.md.
    plate_text = Column(String, unique=True, index=True, nullable=True)
    plate_confidence = Column(Float, default=0.0)
    # Ever corroborated across frames (plate_tracker.has_consensus). Kept apart
    # from plate_confidence: on the benchmark confidence doesn't separate right
    # from wrong reads (0.262-0.990 vs 0.260-0.956), so this is independent
    # evidence. Only ratchets up. NULL on old rows = not corroborated, which
    # can't buy a CRITICAL (rules_engine.py).
    plate_corroborated = Column(Boolean, default=False)
    vehicle_type = Column(String, default="")
    color = Column(String, default="")
    first_seen = Column(DateTime, default=datetime.utcnow)
    # indexed, default sort of the vehicles list and the control room
    last_seen = Column(DateTime, default=datetime.utcnow, index=True)
    watchlist_flag = Column(Boolean, default=False)


class Plate(Base):
    """A plate read, and in V2 a vehicle sighting: one row per (camera,
    vehicle track), not per OCR frame.

    Pre-V2 a car waiting at a signal made dozens of identical rows, and since
    correlate.get_route() builds the journey from these rows, dozens of fake
    hops. Now the row is created on the first confident read and updated
    (confidence, reads_count, last_seen) while the track stays in frame
    (pipeline/plate_tracker.py)."""
    __tablename__ = "plates"
    id = Column(String, primary_key=True, default=lambda: uid("plt"))
    vehicle_id = Column(String, ForeignKey("vehicles.id"), nullable=True, index=True)
    camera_id = Column(String, ForeignKey("cameras.id"), nullable=False, index=True)
    detection_id = Column(String, ForeignKey("detections.id"), nullable=True)
    plate_text_raw = Column(String, default="")
    plate_text_normalized = Column(String, default="", index=True)
    confidence = Column(Float, default=0.0)
    timestamp = Column(DateTime, default=datetime.utcnow, index=True)  # PROCESSING time, first confident read
    source_timestamp = Column(DateTime, nullable=True)  # SOURCE time, see Detection.source_timestamp
    snapshot_path = Column(String, nullable=True)
    # V2 sighting fields
    # ByteTrack id as text (matches Detection.track_id). Null without a track
    # id and on pre-V2 rows, never backfilled.
    track_id = Column(String, nullable=True, index=True)
    # last time the track was still seen here. with timestamp = dwell time
    last_seen = Column(DateTime, nullable=True)
    # passing reads that agreed on the text. 1 = single frame
    reads_count = Column(Integer, default=1)
    # the vehicle's YOLO class/confidence at recognition, saves a join
    vehicle_class = Column(String, default="")
    detection_confidence = Column(Float, default=0.0)
    # Full-frame pixel coords. plate_bbox null = no plate region found and OCR
    # read the whole vehicle crop, which is less trustworthy; never filled
    # with the vehicle box.
    vehicle_bbox = Column(JSON, nullable=True)
    plate_bbox = Column(JSON, nullable=True)
    # ANPR review.
    # auto_accepted: cleared the gate, nothing to do.
    # pending_review: kept but below the review floor, or uncorroborated, or
    #   below the watchlist floor on a watchlist match. waiting on an operator.
    # corrected: operator gave the real text. rejected: not usable.
    review_status = Column(String, default="auto_accepted", index=True)
    reviewed_by = Column(String, ForeignKey("users.id"), nullable=True)
    reviewed_at = Column(DateTime, nullable=True)
    # Operator's text, separate from plate_text_raw (what OCR said) and
    # plate_text_normalized (what the parser made of it).
    corrected_text = Column(String, nullable=True)
    # ANPR explainability. Separate signals, stored unblended; `confidence`
    # stays the OCR engine's own number.
    #
    # preprocessing variant that won ("clahe" by default, plate_preprocess.py).
    # null on older rows
    ocr_variant = Column(String, nullable=True)
    # variants that agreed on the text; 1 unless multi-variant is on
    variants_agreeing = Column(Integer, nullable=True)
    # Enough agreeing frames to settle it (plate_tracker.has_consensus). False
    # is a real but uncorroborated sighting, always pending_review.
    corroborated = Column(Boolean, nullable=True)
    # the plate crop OCR read, for reviewers. only with PLATE_DEBUG_CROPS
    plate_crop_path = Column(String, nullable=True)


class Person(Base):
    __tablename__ = "persons"
    id = Column(String, primary_key=True, default=lambda: uid("prs"))
    first_seen = Column(DateTime, default=datetime.utcnow)
    last_seen = Column(DateTime, default=datetime.utcnow)
    watchlist_flag = Column(Boolean, default=False)
    note = Column(String, default="")


class WatchlistEntry(Base):
    __tablename__ = "watchlist_entries"
    id = Column(String, primary_key=True, default=lambda: uid("wl"))
    entity_type = Column(String, nullable=False)  # person | vehicle | plate
    identifier = Column(String, nullable=False)  # plate text, person note/image ref
    reason = Column(String, default="")
    priority = Column(String, default="MEDIUM")  # LOW | MEDIUM | HIGH | CRITICAL
    added_by = Column(String, ForeignKey("users.id"), nullable=True)
    valid_from = Column(DateTime, default=datetime.utcnow)
    valid_until = Column(DateTime, nullable=True)
    active = Column(Boolean, default=True)


class Zone(Base):
    __tablename__ = "zones"
    id = Column(String, primary_key=True, default=lambda: uid("zone"))
    name = Column(String, nullable=False)
    camera_id = Column(String, ForeignKey("cameras.id"), nullable=False)
    zone_type = Column(String, default="restricted")
    severity = Column(String, default="HIGH")
    # axis-aligned rectangle in normalized 0-1 coords relative to frame
    x1 = Column(Float, default=0.0)
    y1 = Column(Float, default=0.0)
    x2 = Column(Float, default=1.0)
    y2 = Column(Float, default=1.0)
    schedule_start = Column(String, default="00:00")
    schedule_end = Column(String, default="23:59")
    active = Column(Boolean, default=True)
    # Loitering dwell threshold in seconds. Only checked when set AND an
    # active AlertRule(rule_type="loitering") points at the zone
    # (rules_engine.py). Zone entry doesn't care.
    loitering_seconds = Column(Float, nullable=True)


class AlertRule(Base):
    __tablename__ = "alert_rules"
    id = Column(String, primary_key=True, default=lambda: uid("rule"))
    name = Column(String, nullable=False)
    rule_type = Column(String, nullable=False)  # watchlist_plate | zone_entry
    zone_id = Column(String, ForeignKey("zones.id"), nullable=True)
    priority = Column(String, default="HIGH")
    active = Column(Boolean, default=True)
    version = Column(String, default="rules-1.0")


class Alert(Base):
    __tablename__ = "alerts"
    id = Column(String, primary_key=True, default=lambda: uid("alt"))
    camera_id = Column(String, ForeignKey("cameras.id"), nullable=False, index=True)
    rule_id = Column(String, ForeignKey("alert_rules.id"), nullable=True)
    severity = Column(String, default="MEDIUM", index=True)  # LOW | MEDIUM | HIGH | CRITICAL
    status = Column(String, default="new", index=True)  # new | acknowledged | escalated | dismissed
    vehicle_id = Column(String, ForeignKey("vehicles.id"), nullable=True)
    detection_id = Column(String, ForeignKey("detections.id"), nullable=True)
    confidence = Column(Float, default=0.0)
    reasons = Column(JSON, default=list)  # explainability list
    timestamp = Column(DateTime, default=datetime.utcnow, index=True)  # PROCESSING time; the alert list's sort key
    source_timestamp = Column(DateTime, nullable=True)  # SOURCE time of the triggering detection
    acknowledged_by = Column(String, ForeignKey("users.id"), nullable=True)
    snapshot_path = Column(String, nullable=True)
    # 0-100 risk score and its per-factor breakdown (pipeline/risk.py).
    # reasons say what matched, factors say how much each counted.
    risk_score = Column(Integer, default=0, index=True)
    risk_factors = Column(JSON, default=list)
    # Operator feedback / false-positive tracking. Null until someone reviews
    # it, never defaulted to "confirmed".
    feedback = Column(String, nullable=True, index=True)  # confirmed | false_positive | needs_review
    feedback_reason = Column(String, nullable=True)
    feedback_by = Column(String, ForeignKey("users.id"), nullable=True)
    feedback_at = Column(DateTime, nullable=True)


class IncidentAlert(Base):
    """Many alerts -> one incident.

    Incident.alert_id stays the originating alert (callers use it). But a
    watchlisted car entering a zone and then showing up on three more cameras
    is one event with five alerts, not five incidents."""
    __tablename__ = "incident_alerts"
    id = Column(String, primary_key=True, default=lambda: uid("ia"))
    incident_id = Column(String, ForeignKey("incidents.id"), nullable=False, index=True)
    alert_id = Column(String, ForeignKey("alerts.id"), nullable=False, index=True)
    # why this alert was judged part of the incident; it's an inference
    correlation_reason = Column(String, default="")
    created_at = Column(DateTime, default=datetime.utcnow)


class Incident(Base):
    __tablename__ = "incidents"
    id = Column(String, primary_key=True, default=lambda: uid("inc"))
    title = Column(String, nullable=False)
    incident_type = Column(String, default="")
    priority = Column(String, default="MEDIUM")
    status = Column(String, default="open", index=True)  # open | in_progress | closed
    location = Column(String, default="")
    description = Column(String, default="")
    camera_id = Column(String, ForeignKey("cameras.id"), nullable=True)
    alert_id = Column(String, ForeignKey("alerts.id"), nullable=True)
    # indexed with created_at: every CRITICAL looks for an open incident on
    # the same vehicle in the correlation window
    vehicle_id = Column(String, ForeignKey("vehicles.id"), nullable=True, index=True)
    assigned_to = Column(String, ForeignKey("users.id"), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, index=True)
    updated_at = Column(DateTime, default=datetime.utcnow)


class IncidentNote(Base):
    __tablename__ = "incident_notes"
    id = Column(String, primary_key=True, default=lambda: uid("note"))
    incident_id = Column(String, ForeignKey("incidents.id"), nullable=False)
    author_id = Column(String, ForeignKey("users.id"), nullable=True)
    text = Column(String, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)


class Evidence(Base):
    __tablename__ = "evidence"
    id = Column(String, primary_key=True, default=lambda: uid("evd"))
    incident_id = Column(String, ForeignKey("incidents.id"), nullable=True, index=True)
    evidence_type = Column(String, default="snapshot")  # snapshot | clip | report
    camera_id = Column(String, ForeignKey("cameras.id"), nullable=True)
    file_path = Column(String, nullable=True)
    sha256 = Column(String, nullable=True)
    uploaded_by = Column(String, ForeignKey("users.id"), nullable=True)
    verification_status = Column(String, default="unverified")
    created_at = Column(DateTime, default=datetime.utcnow, index=True)
    # event clip linkage. null on older snapshot/report rows
    alert_id = Column(String, ForeignKey("alerts.id"), nullable=True)
    detection_id = Column(String, ForeignKey("detections.id"), nullable=True)
    event_type = Column(String, default="")  # e.g. watchlist_match | zone_entry
    source_timestamp = Column(DateTime, nullable=True)
    # Model/rule versions active at capture, stamped once and never updated.
    # Null on older evidence rather than backfilled with today's version.
    model_version = Column(String, nullable=True)
    rule_version = Column(String, nullable=True)


class AuditLog(Base):
    __tablename__ = "audit_logs"
    id = Column(String, primary_key=True, default=lambda: uid("aud"))
    user_id = Column(String, ForeignKey("users.id"), nullable=True)
    username = Column(String, default="")
    action = Column(String, nullable=False)  # e.g. "login", "POST /api/incidents"
    resource = Column(String, default="")
    result = Column(String, default="SUCCESS")
    ip = Column(String, default="")
    timestamp = Column(DateTime, default=datetime.utcnow, index=True)
    # Tamper-evident chain. id is random, so the chain needs its own position:
    # max(chain_seq)+1 in app/audit.py. Unique so a concurrent write raises
    # instead of mis-ordering (audit.py retries).
    chain_seq = Column(Integer, nullable=True, unique=True, index=True)
    # prev_hash is the previous row's entry_hash ("0"*64 for the first);
    # entry_hash = sha256(prev_hash + this row's canonical fields). Edit or
    # delete a row and every hash after it breaks, which is what verify-chain
    # looks for.
    prev_hash = Column(String, nullable=True)
    entry_hash = Column(String, nullable=True)


class SelfHealEvent(Base):
    """One row per real recovery attempt (app/self_heal/engine.py). Not
    written for routine successes, only when something went wrong and a
    recovery ran. Best-effort diagnostics; losing a row under heavy contention
    is fine and never blocks the operation it describes."""
    __tablename__ = "self_heal_events"
    id = Column(String, primary_key=True, default=lambda: uid("sh"))
    timestamp = Column(DateTime, default=datetime.utcnow, index=True)
    component = Column(String, nullable=False, index=True)  # database | camera | worker | websocket | api | camera_catalog | sentinel_grid
    camera_id = Column(String, ForeignKey("cameras.id"), nullable=True, index=True)
    error_type = Column(String, nullable=False)  # SQLITE_LOCK | STREAM_DECODE_ERROR | CAMERA_TIMEOUT | WORKER_EXCEPTION | MISSING_CONFIG | ...
    severity = Column(String, default="warning")  # info | warning | critical
    message = Column(String, default="")
    recovery_action = Column(String, default="")  # ROLLBACK_RETRY | RECONNECT | RESTART_WORKER | NONE ...
    attempt = Column(Integer, default=1)
    max_attempts = Column(Integer, default=1)
    status = Column(String, default="RECOVERED")  # RECOVERING | RECOVERED | FAILED | CONFIG_REQUIRED
    duration_seconds = Column(Float, default=0.0)
    endpoint = Column(String, default="")
    event_metadata = Column(JSON, default=dict)
