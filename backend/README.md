# SENTINEL VISION — Backend

FastAPI service running the real detection pipeline: YOLOv8 (ultralytics) person/vehicle
detection + ByteTrack tracking, EasyOCR-based ANPR, cross-camera correlation, a zone/watchlist
rules engine producing explainable alerts and incidents, evidence packaging, RBAC, and audit
logging. SQLite is the datastore; WebSocket pushes live alerts/detections to the dashboard.

## Run

```
uv venv --python 3.11 .venv         # already created
uv pip install --python .venv -r requirements.txt --extra-index-url https://download.pytorch.org/whl/cpu
.venv/Scripts/python.exe -m uvicorn app.main:app --reload --port 8000
```

First boot downloads `yolov8n.pt` (~6MB) and EasyOCR's detection/recognition models
(~100MB) — this only happens once per machine.

API docs: http://localhost:8000/docs
Health: http://localhost:8000/api/health

Seeded accounts (see `app/seed.py`), all password `sentinel123`:
`admin` (Administrator), `operator1` (Control Room Operator), `investigator1` (Investigator),
`auditor1` (Auditor).

## Adding a camera source

There is no real CCTV/RTSP/VMS feed available in this environment. Two real sources are
supported instead, both producing genuine frames for the real detection pipeline:

- **webcam** — device index (e.g. `0`)
- **video_file** — path to an uploaded video, looped continuously like a live feed

`rtsp` is a defined-but-unimplemented source type (`app/pipeline/source.py`) so a real
RTSP/ONVIF adapter can be dropped in later without touching detection, ANPR, correlation,
or the rules engine.

## Scope & honesty notes (see also doc's own "AI honesty rule")

- **No Kafka / Kubernetes / vector-DB / edge-Jetson deployment.** Single FastAPI process +
  SQLite is the "working slice" the source master document itself recommends building for a
  hackathon; the scale-out path is documented, not built.
- **No face recognition / person re-identification.** Privacy-sensitive and marked ADVANCED
  in the source doc — the Person module is detection/tracking only.
- **ANPR accuracy is whatever EasyOCR actually reads** off the vehicle crop in real time — no
  accuracy number is hard-coded or floor-clamped. See `/api/analytics/ai-performance` for real
  measured aggregates (not precision/recall — that needs a labeled ground-truth set, which
  isn't available here).
- **Restricted zones are full-camera-frame or axis-aligned rectangles**, not free-form polygon
  drawing.
- **Natural-language search is a regex/keyword parser** to structured filters, not an LLM.
- **JWT secret is a dev default in `config.py`** — replace via `.env` for anything beyond a
  local demo.
- MJPEG stream/snapshot endpoints are unauthenticated (browser `<img>` tags can't send bearer
  headers) — acceptable for a local demo, would need a short-lived signed URL token in
  production.
