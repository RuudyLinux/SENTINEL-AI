"""ORM models — mirrors doc §51 (Database Model) / §7 (Core Data Model)."""
import uuid
from datetime import datetime

from sqlalchemy import (
    Column, String, Float, Integer, Boolean, DateTime, ForeignKey, JSON
)
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
    # Official camera-catalogue linkage (Phase 3). Populated only by a
    # catalogue sync (see pipeline/catalog.py) — null for manually-added
    # cameras. `catalog_stale=True` means the catalogue no longer lists this
    # camera as of the last sync; it is kept (not deleted) so history isn't lost.
    external_catalog_id = Column(String, nullable=True, index=True)
    catalog_codec = Column(String, default="")
    catalog_live_status = Column(String, default="")
    catalog_synced_at = Column(DateTime, nullable=True)
    catalog_stale = Column(Boolean, default=False)
    # Phase 6: the catalogue's other two stream URLs, preserved alongside the
    # RTSP one already used for AI ingestion (source_uri). Genuinely
    # optional — a record missing either stays NULL, never fabricated.
    # Unlike source_uri (may carry embedded RTSP credentials, deliberately
    # never returned by the API), these are meant for direct client
    # consumption per the official spec (WHEP -> browser preview, HLS ->
    # dashboard/mobile fallback) so CameraOut does expose them.
    whep_url = Column(String, nullable=True)
    hls_url = Column(String, nullable=True)
    # Model 2/4: free-form grouping/tagging (e.g. "North Zone", "Highway Cams") —
    # client-side filterable on the Cameras screen. Empty string, never null, so
    # existing rows migrate cleanly (see ensure_columns backfill in main.py).
    # Named `camera_group`, not `group` — the latter is a reserved SQL keyword and
    # broke the raw-SQL ensure_columns/ensure_indexes migration helpers (SQLAlchemy's
    # own query compiler would have auto-quoted it, but those helpers don't).
    camera_group = Column(String, default="")


class Detection(Base):
    __tablename__ = "detections"
    id = Column(String, primary_key=True, default=lambda: uid("det"))
    camera_id = Column(String, ForeignKey("cameras.id"), nullable=False, index=True)
    timestamp = Column(DateTime, default=datetime.utcnow, index=True)  # PROCESSING time: when SENTINEL wrote this row
    source_timestamp = Column(DateTime, nullable=True)  # SOURCE time: when the frame was captured, if reliably known (see pipeline/timing.py)
    cls = Column(String, nullable=False)  # person | car | truck | bus | motorbike
    confidence = Column(Float, nullable=False)
    bbox = Column(JSON, default=list)  # [x1,y1,x2,y2]
    track_id = Column(String, nullable=True)
    model_version = Column(String, default="")
    snapshot_path = Column(String, nullable=True)
    # Cross-camera PERSON correlation (Phase 5): a compact HSV color-histogram
    # signature of the crop, computed only for cls=="person" (pipeline/appearance.py).
    # Explicitly NOT biometric/facial — a visual-similarity signal only, used to rank
    # candidate sightings for an investigator, never an identity claim. Null when not
    # computed (never fabricated) — see worker.py._process_frame.
    appearance_signature = Column(JSON, nullable=True)


class Track(Base):
    __tablename__ = "tracks"
    id = Column(String, primary_key=True, default=lambda: uid("trk"))
    camera_id = Column(String, ForeignKey("cameras.id"), nullable=False)
    cls = Column(String, nullable=False)
    yolo_track_id = Column(Integer, nullable=True)
    first_seen = Column(DateTime, default=datetime.utcnow)
    last_seen = Column(DateTime, default=datetime.utcnow)
    vehicle_id = Column(String, ForeignKey("vehicles.id"), nullable=True)


class Vehicle(Base):
    __tablename__ = "vehicles"
    id = Column(String, primary_key=True, default=lambda: uid("veh"))
    plate_text = Column(String, index=True, nullable=True)
    plate_confidence = Column(Float, default=0.0)
    vehicle_type = Column(String, default="")
    color = Column(String, default="")
    first_seen = Column(DateTime, default=datetime.utcnow)
    last_seen = Column(DateTime, default=datetime.utcnow)
    watchlist_flag = Column(Boolean, default=False)


class Plate(Base):
    __tablename__ = "plates"
    id = Column(String, primary_key=True, default=lambda: uid("plt"))
    vehicle_id = Column(String, ForeignKey("vehicles.id"), nullable=True)
    camera_id = Column(String, ForeignKey("cameras.id"), nullable=False)
    detection_id = Column(String, ForeignKey("detections.id"), nullable=True)
    plate_text_raw = Column(String, default="")
    plate_text_normalized = Column(String, default="", index=True)
    confidence = Column(Float, default=0.0)
    timestamp = Column(DateTime, default=datetime.utcnow)  # PROCESSING time
    source_timestamp = Column(DateTime, nullable=True)  # SOURCE time — see Detection.source_timestamp
    snapshot_path = Column(String, nullable=True)


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
    # Loitering rule support (Phase 6): dwell-time threshold in seconds. Null means
    # no loitering check applies to this zone — only zones with this set AND an
    # active AlertRule(rule_type="loitering") referencing them are checked (see
    # rules_engine.py) — restricted-zone entry itself is unaffected either way.
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
    timestamp = Column(DateTime, default=datetime.utcnow)  # PROCESSING time
    source_timestamp = Column(DateTime, nullable=True)  # SOURCE time of the triggering detection
    acknowledged_by = Column(String, ForeignKey("users.id"), nullable=True)
    snapshot_path = Column(String, nullable=True)


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
    vehicle_id = Column(String, ForeignKey("vehicles.id"), nullable=True)
    assigned_to = Column(String, ForeignKey("users.id"), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
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
    incident_id = Column(String, ForeignKey("incidents.id"), nullable=True)
    evidence_type = Column(String, default="snapshot")  # snapshot | clip | report
    camera_id = Column(String, ForeignKey("cameras.id"), nullable=True)
    file_path = Column(String, nullable=True)
    sha256 = Column(String, nullable=True)
    uploaded_by = Column(String, ForeignKey("users.id"), nullable=True)
    verification_status = Column(String, default="unverified")
    created_at = Column(DateTime, default=datetime.utcnow)
    # Event-clip linkage (Phase 3). Null for pre-existing snapshot/report rows.
    alert_id = Column(String, ForeignKey("alerts.id"), nullable=True)
    detection_id = Column(String, ForeignKey("detections.id"), nullable=True)
    event_type = Column(String, default="")  # e.g. watchlist_match | zone_entry
    source_timestamp = Column(DateTime, nullable=True)


class AuditLog(Base):
    __tablename__ = "audit_logs"
    id = Column(String, primary_key=True, default=lambda: uid("aud"))
    user_id = Column(String, ForeignKey("users.id"), nullable=True)
    username = Column(String, default="")
    action = Column(String, nullable=False)  # e.g. "login", "POST /api/incidents"
    resource = Column(String, default="")
    result = Column(String, default="SUCCESS")
    ip = Column(String, default="")
    timestamp = Column(DateTime, default=datetime.utcnow)


class SelfHealEvent(Base):
    """Sentinel Self-Heal audit trail — one row per real recovery attempt
    (see app/self_heal/engine.py). Deliberately NOT written for every routine
    successful commit/read (that would be thousands of rows/minute with zero
    diagnostic value) — only when something actually went wrong and a
    recovery path (retry, reconnect, restart, rollback...) ran. Never the
    system of record for correctness — a best-effort diagnostic/audit log;
    losing an occasional row under extreme contention is acceptable and
    never blocks the real operation it's describing."""
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
