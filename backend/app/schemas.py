"""Pydantic request/response schemas."""
from datetime import datetime
from typing import Optional, List
from pydantic import BaseModel, ConfigDict


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

    model_config = ConfigDict(from_attributes=True)


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

    model_config = ConfigDict(from_attributes=True)


class CameraCreate(BaseModel):
    camera_code: str
    name: str
    department: str = "Police"
    location: str = ""
    lat: float = 0.0
    lng: float = 0.0
    source_type: str  # webcam | video_file | rtsp | mock_vms | onvif (interface stub — see pipeline/adapters.py)
    source_uri: str  # "0" for webcam index, filename for video_file, url for rtsp
    ai_person: bool = True
    ai_vehicle: bool = True
    ai_anpr: bool = True
    camera_group: str = ""  # free-form grouping/tag, e.g. "North Zone" — client-side filterable


class CameraUpdate(BaseModel):
    """PATCH payload — every field optional, only fields actually present in the
    request are applied (see routers/cameras.py `model_dump(exclude_unset=True)`).
    Deliberately excludes source_type/source_uri: changing a camera's source is a
    reconnect operation (stop/re-add), not an in-place edit."""
    name: Optional[str] = None
    location: Optional[str] = None
    camera_group: Optional[str] = None
    lat: Optional[float] = None
    lng: Optional[float] = None
    ai_person: Optional[bool] = None
    ai_vehicle: Optional[bool] = None
    ai_anpr: Optional[bool] = None


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
    camera_group: str = ""
    last_frame_at: Optional[datetime] = None
    # Richer connection-lifecycle state (24/7 auto-connect task) — in-memory
    # only (CAMERA_STATS), attached by routers/cameras.py.list_cameras; null
    # for a camera whose worker has never run in this process. Distinct from
    # `status` (DB column, only ever online/offline/degraded).
    grid_state: Optional[str] = None
    # Same in-memory source (CAMERA_STATS), same reasoning as grid_state above —
    # already computed by worker.py per iteration, just not previously exposed.
    # Null for a camera whose worker has never run in this process.
    reconnect_count: Optional[int] = None
    last_error: Optional[str] = None
    # Catalogue linkage — informational only. Deliberately no `source_uri`
    # here: the RTSP URL may carry embedded credentials and must never reach
    # the frontend/logs (see P0-E from Phase 1 and pipeline/catalog.py).
    external_catalog_id: Optional[str] = None
    catalog_codec: str = ""
    catalog_live_status: str = ""
    catalog_synced_at: Optional[datetime] = None
    catalog_stale: bool = False
    # Unlike source_uri, these ARE meant for the client — WHEP is for a
    # browser preview player, HLS for dashboard/mobile/restricted-network
    # fallback (per the official spec). Neither is required; both stay
    # null when the catalogue didn't supply one.
    whep_url: Optional[str] = None
    hls_url: Optional[str] = None

    model_config = ConfigDict(from_attributes=True)


class DetectionOut(BaseModel):
    id: str
    camera_id: str
    timestamp: datetime  # PROCESSING time
    source_timestamp: Optional[datetime] = None  # SOURCE time — see models.Detection
    cls: str
    confidence: float
    bbox: List[float]
    track_id: Optional[str] = None
    snapshot_path: Optional[str] = None

    model_config = ConfigDict(from_attributes=True)


class PlateOut(BaseModel):
    id: str
    vehicle_id: Optional[str] = None
    camera_id: str
    plate_text_normalized: str
    confidence: float
    timestamp: datetime
    source_timestamp: Optional[datetime] = None
    snapshot_path: Optional[str] = None

    model_config = ConfigDict(from_attributes=True)


class VehicleOut(BaseModel):
    id: str
    plate_text: Optional[str] = None
    plate_confidence: float
    vehicle_type: str
    color: str
    first_seen: datetime
    last_seen: datetime
    watchlist_flag: bool

    model_config = ConfigDict(from_attributes=True)


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

    model_config = ConfigDict(from_attributes=True)


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
    loitering_seconds: Optional[float] = None  # None = no loitering check on this zone


class ZoneOut(ZoneCreate):
    id: str
    active: bool

    model_config = ConfigDict(from_attributes=True)


class AlertRuleCreate(BaseModel):
    name: str
    rule_type: str  # watchlist_plate | zone_entry | loitering
    zone_id: Optional[str] = None
    priority: str = "HIGH"


class AlertRuleOut(AlertRuleCreate):
    id: str
    active: bool
    version: str

    model_config = ConfigDict(from_attributes=True)


class AlertOut(BaseModel):
    id: str
    camera_id: str
    severity: str
    status: str
    vehicle_id: Optional[str] = None
    confidence: float
    reasons: List[str]
    timestamp: datetime
    source_timestamp: Optional[datetime] = None
    snapshot_path: Optional[str] = None

    model_config = ConfigDict(from_attributes=True)


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

    model_config = ConfigDict(from_attributes=True)


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
    alert_id: Optional[str] = None
    detection_id: Optional[str] = None
    event_type: str = ""
    source_timestamp: Optional[datetime] = None

    model_config = ConfigDict(from_attributes=True)


class AuditOut(BaseModel):
    id: str
    username: str
    action: str
    resource: str
    result: str
    timestamp: datetime

    model_config = ConfigDict(from_attributes=True)
