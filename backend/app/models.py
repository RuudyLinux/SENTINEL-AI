"""ORM models — mirrors doc §51 (Database Model) / §7 (Core Data Model)."""
import uuid
from datetime import datetime

from sqlalchemy import (
    Column, String, Float, Integer, Boolean, DateTime, ForeignKey, Text, JSON
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


class Detection(Base):
    __tablename__ = "detections"
    id = Column(String, primary_key=True, default=lambda: uid("det"))
    camera_id = Column(String, ForeignKey("cameras.id"), nullable=False)
    timestamp = Column(DateTime, default=datetime.utcnow)
    cls = Column(String, nullable=False)  # person | car | truck | bus | motorbike
    confidence = Column(Float, nullable=False)
    bbox = Column(JSON, default=list)  # [x1,y1,x2,y2]
    track_id = Column(String, nullable=True)
    model_version = Column(String, default="")
    snapshot_path = Column(String, nullable=True)


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
    timestamp = Column(DateTime, default=datetime.utcnow)
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
    camera_id = Column(String, ForeignKey("cameras.id"), nullable=False)
    rule_id = Column(String, ForeignKey("alert_rules.id"), nullable=True)
    severity = Column(String, default="MEDIUM")  # LOW | MEDIUM | HIGH | CRITICAL
    status = Column(String, default="new")  # new | acknowledged | escalated | dismissed
    vehicle_id = Column(String, ForeignKey("vehicles.id"), nullable=True)
    detection_id = Column(String, ForeignKey("detections.id"), nullable=True)
    confidence = Column(Float, default=0.0)
    reasons = Column(JSON, default=list)  # explainability list
    timestamp = Column(DateTime, default=datetime.utcnow)
    acknowledged_by = Column(String, ForeignKey("users.id"), nullable=True)
    snapshot_path = Column(String, nullable=True)


class Incident(Base):
    __tablename__ = "incidents"
    id = Column(String, primary_key=True, default=lambda: uid("inc"))
    title = Column(String, nullable=False)
    incident_type = Column(String, default="")
    priority = Column(String, default="MEDIUM")
    status = Column(String, default="open")  # open | in_progress | closed
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
