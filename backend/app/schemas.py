"""Pydantic request/response schemas."""
from datetime import datetime
from typing import Optional, List
from pydantic import BaseModel, ConfigDict, Field, field_validator


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
    source_type: str  # webcam | video_file | rtsp | mock_vms | onvif (stub, see pipeline/adapters.py)
    source_uri: str  # "0" for webcam index, filename for video_file, url for rtsp
    ai_person: bool = True
    ai_vehicle: bool = True
    ai_anpr: bool = True
    camera_group: str = ""  # free-form tag like "North Zone", filtered client side


class NearbyCameraOut(BaseModel):
    id: str
    camera_code: str
    name: str
    location: str
    lat: float
    lng: float
    status: str
    distance_m: float


class CameraUpdate(BaseModel):
    """PATCH payload; only fields present are applied. The source can't be
    edited: changing it is a reconnect."""
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
    retired: bool = False
    # AI is on but no AI slot is free (pipeline/ai_capacity.py)
    ai_blocked: bool = False
    # REC button running for this camera (pipeline/recorder.py)
    recording: bool = False
    last_frame_at: Optional[datetime] = None
    # In-memory lifecycle state (CAMERA_STATS), null if the worker never ran.
    # Distinct from `status`, the DB column (online/offline/degraded).
    grid_state: Optional[str] = None
    reconnect_count: Optional[int] = None
    last_error: Optional[str] = None
    # Catalogue linkage, informational. No source_uri on purpose: the RTSP URL
    # can carry credentials and must never reach the frontend or logs.
    external_catalog_id: Optional[str] = None
    catalog_codec: str = ""
    catalog_live_status: str = ""
    catalog_synced_at: Optional[datetime] = None
    catalog_stale: bool = False
    # These are meant for the client: WHEP for the browser player, HLS as the
    # fallback. Null when the catalogue has none.
    whep_url: Optional[str] = None
    hls_url: Optional[str] = None

    model_config = ConfigDict(from_attributes=True)


class DetectionOut(BaseModel):
    id: str
    camera_id: str
    timestamp: datetime  # PROCESSING time
    source_timestamp: Optional[datetime] = None  # SOURCE time, see models.Detection
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
    # V2 sighting fields. Optional, pre-V2 rows never had them and stay null
    track_id: Optional[str] = None
    last_seen: Optional[datetime] = None
    reads_count: int = 1
    vehicle_class: str = ""
    detection_confidence: float = 0.0
    vehicle_bbox: Optional[List[float]] = None
    plate_bbox: Optional[List[float]] = None
    # ANPR review
    review_status: str = "auto_accepted"
    reviewed_by: Optional[str] = None
    reviewed_at: Optional[datetime] = None
    corrected_text: Optional[str] = None
    # ANPR explainability, separate from `confidence`. Null on older rows.
    ocr_variant: Optional[str] = None
    variants_agreeing: Optional[int] = None
    corroborated: Optional[bool] = None
    plate_crop_path: Optional[str] = None

    model_config = ConfigDict(from_attributes=True)


class PlateReviewCorrectRequest(BaseModel):
    corrected_text: str


class PlateReviewRejectRequest(BaseModel):
    reason: Optional[str] = None


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
    """One hop in a vehicle's journey across cameras: where and when a camera saw
    it. A camera-to-camera path, not a GPS track (no interpolated position,
    heading or speed).
    """
    camera_id: str
    camera_code: str
    camera_name: str
    timestamp: datetime  # first confident recognition at this camera
    confidence: float
    snapshot_path: Optional[str] = None
    # V2 additions, defaulted so pre-V2 callers still work
    location: str = ""
    lat: float = 0.0
    lng: float = 0.0
    first_seen: Optional[datetime] = None
    last_seen: Optional[datetime] = None
    dwell_seconds: float = 0.0
    reads_count: int = 1
    track_id: Optional[str] = None
    vehicle_class: str = ""
    plate_id: Optional[str] = None
    detection_id: Optional[str] = None


class VehicleRouteOut(BaseModel):
    vehicle: VehicleOut
    sightings: List[SightingOut]


class VehicleSummaryOut(BaseModel):
    """Investigation header for one vehicle, shown before the journey,
    evidence and alerts."""
    vehicle: VehicleOut
    total_sightings: int
    cameras_visited: int
    first_seen: Optional[datetime] = None
    last_seen: Optional[datetime] = None
    current_camera_id: Optional[str] = None
    current_camera_code: Optional[str] = None
    current_camera_name: Optional[str] = None
    current_seen_at: Optional[datetime] = None
    # only when the latest sighting is inside the live window: "on camera
    # now" vs "last seen here"
    is_live: bool = False
    alert_count: int = 0
    incident_count: int = 0
    evidence_count: int = 0
    watchlist_flag: bool = False
    best_plate_confidence: float = 0.0
    # from pipeline/risk.py
    risk_score: int = 0
    risk_severity: str = "LOW"
    risk_factors: List[dict] = []


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
    # the detection that fired the rule
    detection_id: Optional[str] = None
    confidence: float
    # Coerced so a NULL in these nullable JSON columns becomes an empty list
    # instead of failing validation for the whole response.
    reasons: List[str] = []
    timestamp: datetime
    source_timestamp: Optional[datetime] = None
    snapshot_path: Optional[str] = None
    # pipeline/risk.py. Pre-V2 alerts have 0 / [], no assessment was made
    # back then and we don't invent one now
    risk_score: int = 0
    risk_factors: List[dict] = []

    @field_validator("reasons", "risk_factors", mode="before")
    @classmethod
    def _null_json_is_empty(cls, value):
        return [] if value is None else value
    # false-positive feedback, null = not reviewed yet
    feedback: Optional[str] = None
    feedback_reason: Optional[str] = None
    feedback_at: Optional[datetime] = None

    model_config = ConfigDict(from_attributes=True)


class AlertFeedbackRequest(BaseModel):
    feedback: str  # confirmed | false_positive | needs_review
    reason: Optional[str] = None


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
    # Bounded so a single note can't be arbitrarily large.
    text: str = Field(min_length=1, max_length=5000)


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
    # versions at capture. null on older evidence, not backfilled
    model_version: Optional[str] = None
    rule_version: Optional[str] = None

    model_config = ConfigDict(from_attributes=True)


class PurgeExpiredRequest(BaseModel):
    # actually deleting needs dry_run=False AND confirm=True, one flag flip
    # shouldn't destroy evidence
    dry_run: bool = True
    confirm: bool = False


class AuditOut(BaseModel):
    id: str
    username: str
    action: str
    resource: str
    result: str
    timestamp: datetime

    model_config = ConfigDict(from_attributes=True)
