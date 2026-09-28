"""SQLAlchemy ORM models."""
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
    # Retired cameras are kept so their history still resolves; they never
    # connect again until reinstated.
    retired = Column(Boolean, default=False, nullable=False, server_default=sa_false())
    # Client-facing stream URLs from the catalogue (WHEP preview, HLS fallback).
    # Unlike source_uri, which may embed credentials, these are exposed by the API.
    whep_url = Column(String, nullable=True)
    hls_url = Column(String, nullable=True)
    # Free-form grouping ("North Zone"). Not named `group`, a reserved word the
    # raw-SQL ensure_* helpers don't quote.
    camera_group = Column(String, default="")


class Detection(Base):
    __tablename__ = "detections"
    # Serves the live page's "newest detections for a camera" query.
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
    # HSV colour-histogram signature for persons (appearance.py): visual
    # similarity for ranking leads, not biometric. Null when not computed.
    appearance_signature = Column(JSON, nullable=True)


class Track(Base):
    """One tracked object's lifetime on one camera. ByteTrack ids are unique only
    per camera, so they're stored with camera_id. vehicle_id is filled in once
    the track's plate is read."""
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
    # Unique so two workers seeing a new plate at once can't create two vehicles;
    # the second insert is merged by correlate.upsert_vehicle_for_plate. SQLite
    # databases upgraded in place via ensure_columns lack the constraint (see
    # docs/THREAT_MODEL.md).
    plate_text = Column(String, unique=True, index=True, nullable=True)
    plate_confidence = Column(Float, default=0.0)
    # Whether the plate was ever corroborated across frames. Independent of
    # plate_confidence and only ever set, never cleared. NULL = not corroborated,
    # which can't produce a CRITICAL alert.
    plate_corroborated = Column(Boolean, default=False)
    vehicle_type = Column(String, default="")
    color = Column(String, default="")
    first_seen = Column(DateTime, default=datetime.utcnow)
    # indexed, default sort of the vehicles list and the control room
    last_seen = Column(DateTime, default=datetime.utcnow, index=True)
    watchlist_flag = Column(Boolean, default=False)


class Plate(Base):
    """A plate read; in V2 a vehicle sighting, one row per (camera, track)
    rather than per OCR frame, updated while the track stays in view
    (plate_tracker.py). The vehicle's route is built from these rows."""
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
    # V2 sighting fields. track_id matches Detection.track_id; null without a
    # track and on pre-V2 rows.
    track_id = Column(String, nullable=True, index=True)
    # last time the track was still seen here. with timestamp = dwell time
    last_seen = Column(DateTime, nullable=True)
    # passing reads that agreed on the text. 1 = single frame
    reads_count = Column(Integer, default=1)
    # the vehicle's YOLO class/confidence at recognition, saves a join
    vehicle_class = Column(String, default="")
    detection_confidence = Column(Float, default=0.0)
    # Full-frame pixel coordinates. plate_bbox is null when OCR read the whole
    # vehicle crop, which is less trustworthy.
    vehicle_bbox = Column(JSON, nullable=True)
    plate_bbox = Column(JSON, nullable=True)
    # ANPR review status: auto_accepted, pending_review (below the review floor
    # or uncorroborated), corrected (operator supplied the text) or rejected.
    review_status = Column(String, default="auto_accepted", index=True)
    reviewed_by = Column(String, ForeignKey("users.id"), nullable=True)
    reviewed_at = Column(DateTime, nullable=True)
    # Operator's text, separate from plate_text_raw (what OCR said) and
    # plate_text_normalized (what the parser made of it).
    corrected_text = Column(String, nullable=True)
    # ANPR explainability signals, stored separately from `confidence`.
    # Winning preprocessing variant (plate_preprocess.py); null on older rows.
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
    # Loitering threshold in seconds; applies only when an active loitering rule
    # targets this zone.
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
    """Links alerts to an incident. Incident.alert_id stays the originating
    alert; correlated alerts from other cameras are linked here."""
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
    # Chain position (ids are random). Unique so a concurrent append fails and
    # audit.py retries instead of mis-ordering.
    chain_seq = Column(Integer, nullable=True, unique=True, index=True)
    # entry_hash = sha256(prev_hash + canonical fields), prev_hash being the
    # previous row's hash ("0"*64 for the first). Any edit breaks the chain.
    prev_hash = Column(String, nullable=True)
    entry_hash = Column(String, nullable=True)


class SelfHealEvent(Base):
    """One row per recovery attempt (self_heal/engine.py). Best-effort
    diagnostics: a lost row never blocks the operation it describes."""
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
