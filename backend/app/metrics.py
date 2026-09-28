"""Prometheus metrics for the pipeline and platform.

Counters and histograms are updated where events happen; gauges are sampled at
scrape time from existing state (CAMERA_STATS, RUNNING, WebSocket clients,
psutil).
"""
import logging

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram, generate_latest

logger = logging.getLogger("sentinel.metrics")

# own registry, so library metrics stay out and output is deterministic for tests
REGISTRY = CollectorRegistry()

# pipeline throughput
DETECTIONS_TOTAL = Counter(
    "sentinel_detections_total", "Object detections persisted.",
    ["camera_code", "cls"], registry=REGISTRY,
)
PLATE_OCR_ATTEMPTS = Counter(
    "sentinel_plate_ocr_attempts_total",
    "OCR passes run over a vehicle or plate crop.",
    ["camera_code"], registry=REGISTRY,
)
PLATE_OCR_ACCEPTED = Counter(
    "sentinel_plate_ocr_accepted_total",
    "OCR reads that passed the ANPR quality gate (plausible format AND sufficient confidence).",
    ["camera_code"], registry=REGISTRY,
)
PLATE_OCR_REJECTED = Counter(
    "sentinel_plate_ocr_rejected_total",
    "OCR reads that FAILED the ANPR quality gate, by reason: implausible format "
    "(including an unknown state code), below the confidence floor, too few "
    "preprocessing variants agreeing, or nothing read at all. The counterpart to "
    "ocr_accepted — together they say what the gate is actually doing.",
    ["camera_code", "reason"], registry=REGISTRY,
)
PLATE_DETECT_ATTEMPTS = Counter(
    "sentinel_plate_detect_attempts_total",
    "Vehicle crops submitted to plate detection.",
    ["camera_code"], registry=REGISTRY,
)
PLATE_LOCALIZED = Counter(
    "sentinel_plate_localized_total",
    "Vehicle crops in which an actual plate region was found. The gap against "
    "detect_attempts is the fallback-to-whole-crop rate — the honest measure of "
    "how often localization is carrying the read.",
    ["camera_code", "source"], registry=REGISTRY,
)
PLATE_CONSENSUS_REACHED = Counter(
    "sentinel_plate_consensus_total",
    "Tracked vehicles whose plate reached temporal consensus (enough agreeing "
    "observations to be persisted as a trusted sighting) versus those persisted "
    "as uncorroborated observations awaiting review.",
    ["camera_code", "outcome"], registry=REGISTRY,
)
VEHICLE_SIGHTINGS = Counter(
    "sentinel_vehicle_sightings_total", "Vehicle sighting rows created or updated.",
    ["camera_code"], registry=REGISTRY,
)
ALERTS_TOTAL = Counter(
    "sentinel_alerts_total", "Alerts raised.", ["camera_code", "severity"], registry=REGISTRY,
)
INCIDENTS_TOTAL = Counter(
    "sentinel_incidents_total", "Incidents, split by whether they were newly opened or correlated "
    "into an existing one.", ["outcome"], registry=REGISTRY,
)
SELF_HEAL_EVENTS = Counter(
    "sentinel_self_heal_events_total", "Self-heal recovery events.",
    ["component", "status"], registry=REGISTRY,
)
CAMERA_RECONNECTS = Counter(
    "sentinel_camera_reconnects_total", "Camera stream reconnect attempts.",
    ["camera_code"], registry=REGISTRY,
)

# latency. buckets fit real costs here: CPU YOLO and OCR in the hundreds of
# ms, a SQLite write under 10ms unless contended
INFERENCE_SECONDS = Histogram(
    "sentinel_inference_seconds", "YOLO detection + tracking wall time per frame.",
    ["camera_code"], registry=REGISTRY,
    buckets=(0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0),
)
OCR_SECONDS = Histogram(
    "sentinel_ocr_seconds", "Plate OCR wall time per pass.", ["camera_code"], registry=REGISTRY,
    buckets=(0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0),
)
PLATE_DETECT_SECONDS = Histogram(
    "sentinel_plate_detect_seconds",
    "Plate LOCALIZATION wall time per vehicle crop, separate from OCR so the two "
    "costs can be attributed independently.",
    ["camera_code"], registry=REGISTRY,
    buckets=(0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0),
)

# ANPR signals are separate series (OCR confidence, plate-detect confidence,
# variant agreement); none of them is accuracy, which needs ground truth.
OCR_CONFIDENCE = Histogram(
    "sentinel_ocr_confidence",
    "OCR confidence of gate-passing reads, as reported by the engine. NOT accuracy.",
    ["camera_code"], registry=REGISTRY,
    buckets=(0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0),
)
PLATE_DETECT_CONFIDENCE = Histogram(
    "sentinel_plate_detect_confidence",
    "Confidence of accepted plate-region detections. Labelled by source because a "
    "trained model's probability and the classical localizer's geometric "
    "plausibility score are different quantities and must never be pooled.",
    ["camera_code", "source"], registry=REGISTRY,
    buckets=(0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0),
)
PLATE_VARIANTS_AGREEING = Histogram(
    "sentinel_plate_variants_agreeing",
    "How many preprocessing variants produced the selected read. Always 1 when "
    "multi-variant preprocessing is disabled (the default).",
    ["camera_code"], registry=REGISTRY,
    buckets=(1, 2, 3, 4, 5, 6, 7),
)
DB_WRITE_SECONDS = Histogram(
    "sentinel_db_write_seconds", "Database commit/flush wall time, including retries.",
    ["operation"], registry=REGISTRY,
    buckets=(0.001, 0.005, 0.01, 0.05, 0.1, 0.5, 1.0, 5.0),
)
DB_LOCK_RETRIES = Counter(
    "sentinel_db_lock_retries_total", "Write retries caused by a locked database.",
    registry=REGISTRY,
)

# sampled gauges
CAMERAS_RUNNING = Gauge("sentinel_cameras_running", "Camera worker tasks currently running.", registry=REGISTRY)
CAMERAS_BY_STATE = Gauge(
    "sentinel_cameras_by_state", "Cameras per connection-lifecycle state.", ["state"], registry=REGISTRY,
)
WS_CLIENTS = Gauge("sentinel_websocket_clients", "Connected dashboard WebSocket clients.", registry=REGISTRY)
CAMERA_FPS = Gauge("sentinel_camera_fps", "Frames per second read from a camera.", ["camera_code"], registry=REGISTRY)
CAMERA_INFERENCE_MS = Gauge(
    "sentinel_camera_inference_ms", "Smoothed per-camera inference latency (EMA).",
    ["camera_code"], registry=REGISTRY,
)
CAMERA_READ_MS = Gauge(
    "sentinel_camera_read_ms", "Smoothed per-camera frame-read latency (EMA).",
    ["camera_code"], registry=REGISTRY,
)
PROCESS_CPU_PERCENT = Gauge("sentinel_process_cpu_percent", "Backend process CPU usage.", registry=REGISTRY)
PROCESS_MEMORY_BYTES = Gauge("sentinel_process_memory_bytes", "Backend process resident memory.", registry=REGISTRY)

# GPU memory is registered on the first scrape that finds CUDA, not at
# import. A Gauge exports 0.0 as soon as it exists, and "gpu memory 0" on a
# CPU-only host reads as an idle GPU instead of no GPU.
_GPU_MEMORY_BYTES: "Gauge | None" = None


def _sample_gauges() -> None:
    """Refresh gauges from live state, at scrape time. Every failure is
    contained; a partial scrape beats a 500 from the health endpoint."""
    from .pipeline.worker import CAMERA_STATS, RUNNING
    from .ws import manager

    try:
        CAMERAS_RUNNING.set(sum(1 for task in RUNNING.values() if task and not task.done()))
        WS_CLIENTS.set(len(manager.active))

        by_state: dict[str, int] = {}
        for stats in CAMERA_STATS.values():
            state = str(stats.get("grid_state") or "UNKNOWN")
            by_state[state] = by_state.get(state, 0) + 1
        CAMERAS_BY_STATE.clear()
        for state, count in by_state.items():
            CAMERAS_BY_STATE.labels(state=state).set(count)

        CAMERA_FPS.clear()
        CAMERA_INFERENCE_MS.clear()
        CAMERA_READ_MS.clear()
        for stats in CAMERA_STATS.values():
            code = str(stats.get("camera_code") or "unknown")
            loop_gap = stats.get("loop_gap_ms_ema")
            # fps from the real loop interval, not what the camera advertises
            if loop_gap:
                CAMERA_FPS.labels(camera_code=code).set(1000.0 / loop_gap)
            if stats.get("inference_ms_ema") is not None:
                CAMERA_INFERENCE_MS.labels(camera_code=code).set(stats["inference_ms_ema"])
            if stats.get("read_ms_ema") is not None:
                CAMERA_READ_MS.labels(camera_code=code).set(stats["read_ms_ema"])
    except Exception:
        logger.exception("metrics: sampling camera/websocket gauges failed")

    try:
        import psutil

        process = psutil.Process()
        PROCESS_CPU_PERCENT.set(process.cpu_percent(interval=None))
        PROCESS_MEMORY_BYTES.set(process.memory_info().rss)
    except Exception:
        logger.exception("metrics: sampling process gauges failed")

    global _GPU_MEMORY_BYTES
    try:
        import torch

        if torch.cuda.is_available():
            if _GPU_MEMORY_BYTES is None:
                _GPU_MEMORY_BYTES = Gauge(
                    "sentinel_gpu_memory_allocated_bytes",
                    "GPU memory allocated by torch.",
                    registry=REGISTRY,
                )
            _GPU_MEMORY_BYTES.set(torch.cuda.memory_allocated())
    except Exception:
        # no torch/CUDA/driver: no series at all
        pass


def render() -> bytes:
    """Prometheus text exposition of the current state."""
    _sample_gauges()
    return generate_latest(REGISTRY)
