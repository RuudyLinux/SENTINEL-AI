"""Pydantic request/response schemas."""
from datetime import datetime
from typing import Optional, List, Any
from pydantic import BaseModel


class LoginRequest(BaseModel):
    username: str
    password: str
    department: Optional[str] = None


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: dict


class UserOut(BaseModel):
    id: str
    username: str
    full_name: str
    department: str
    role: Optional[str] = None
    active: bool

    class Config:
        from_attributes = True


class UserCreate(BaseModel):
    username: str
    password: str
    full_name: str = ""
    department: str = ""
    role_name: str = "Control Room Operator"


class RoleOut(BaseModel):
    id: str
    name: str
    description: str

    class Config:
        from_attributes = True


class CameraCreate(BaseModel):
    camera_code: str
    name: str
    department: str = "Police"
    location: str = ""
    lat: float = 0.0
    lng: float = 0.0
    source_type: str  # webcam | video_file | rtsp
    source_uri: str  # "0" for webcam index, filename for video_file, url for rtsp
    ai_person: bool = True
    ai_vehicle: bool = True
    ai_anpr: bool = True


class CameraOut(BaseModel):
    id: str
    camera_code: str
    name: str
    department: str
    location: str
    lat: float
    lng: float
    source_type: str
    status: str
    fps: float
    resolution: str
    latency_ms: float
    error_count: int
    ai_person: bool
    ai_vehicle: bool
    ai_anpr: bool
    last_frame_at: Optional[datetime] = None

    class Config:
        from_attributes = True


class DetectionOut(BaseModel):
    id: str
    camera_id: str
    timestamp: datetime
    cls: str
    confidence: float
    bbox: List[float]
    track_id: Optional[str] = None
    snapshot_path: Optional[str] = None

    class Config:
        from_attributes = True


class PlateOut(BaseModel):
    id: str
    vehicle_id: Optional[str] = None
    camera_id: str
    plate_text_normalized: str
    confidence: float
    timestamp: datetime
    snapshot_path: Optional[str] = None

    class Config:
        from_attributes = True


class VehicleOut(BaseModel):
    id: str
    plate_text: Optional[str] = None
    plate_confidence: float
    vehicle_type: str
    color: str
    first_seen: datetime
    last_seen: datetime
    watchlist_flag: bool

    class Config:
        from_attributes = True


class SightingOut(BaseModel):
    camera_id: str
    camera_code: str
    camera_name: str
    timestamp: datetime
    confidence: float
    snapshot_path: Optional[str] = None


class VehicleRouteOut(BaseModel):
    vehicle: VehicleOut
    sightings: List[SightingOut]


class WatchlistCreate(BaseModel):
    entity_type: str
    identifier: str
    reason: str = ""
    priority: str = "MEDIUM"
    valid_until: Optional[datetime] = None


class WatchlistOut(BaseModel):
    id: str
    entity_type: str
    identifier: str
    reason: str
    priority: str
    active: bool
    valid_from: datetime
    valid_until: Optional[datetime] = None

    class Config:
        from_attributes = True


class ZoneCreate(BaseModel):
    name: str
    camera_id: str
    zone_type: str = "restricted"
    severity: str = "HIGH"
    x1: float = 0.0
    y1: float = 0.0
    x2: float = 1.0
    y2: float = 1.0
    schedule_start: str = "00:00"
    schedule_end: str = "23:59"


class ZoneOut(ZoneCreate):
    id: str
    active: bool

    class Config:
        from_attributes = True


class AlertRuleCreate(BaseModel):
    name: str
    rule_type: str  # watchlist_plate | zone_entry
    zone_id: Optional[str] = None
    priority: str = "HIGH"


class AlertRuleOut(AlertRuleCreate):
    id: str
    active: bool
    version: str

    class Config:
        from_attributes = True


class AlertOut(BaseModel):
    id: str
    camera_id: str
    severity: str
    status: str
    vehicle_id: Optional[str] = None
    confidence: float
    reasons: List[str]
    timestamp: datetime
    snapshot_path: Optional[str] = None

    class Config:
        from_attributes = True


class IncidentCreate(BaseModel):
    title: str
    incident_type: str = ""
    priority: str = "MEDIUM"
    location: str = ""
    description: str = ""
    camera_id: Optional[str] = None
    alert_id: Optional[str] = None
    vehicle_id: Optional[str] = None


class IncidentOut(BaseModel):
    id: str
    title: str
    incident_type: str
    priority: str
    status: str
    location: str
    description: str
    camera_id: Optional[str] = None
    alert_id: Optional[str] = None
    vehicle_id: Optional[str] = None
    assigned_to: Optional[str] = None
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class IncidentNoteCreate(BaseModel):
    text: str


class EvidenceOut(BaseModel):
    id: str
    incident_id: Optional[str] = None
    evidence_type: str
    camera_id: Optional[str] = None
    file_path: Optional[str] = None
    sha256: Optional[str] = None
    verification_status: str
    created_at: datetime

    class Config:
        from_attributes = True


class AuditOut(BaseModel):
    id: str
    username: str
    action: str
    resource: str
    result: str
    timestamp: datetime

    class Config:
        from_attributes = True
