# SENTINEL VISION: Live Detection Proof Video

**File:** `SENTINEL_VISION_Live_Detection_Proof.mp4` (H.264, 1600×900, 25 fps, 1 min 32 s, about 6.3 MB)

## Purpose

Proof for the government challenge submission that SENTINEL VISION runs and detects in real time. The video is a screen recording of the running application in a browser. No screen is mocked, no detection is drawn by hand, and no database record was created outside the application's own forms and pipeline.

## Recording details

| Item | Value |
|---|---|
| Recorded | 29 September 2026, about 01:50 IST, on the development laptop (RTX 3050 Ti, Windows 11) |
| System version | Branch `demo-readiness-phase2` (PR #2), commit `795ebc843f34c63062dd7283eaf6bfe8e27cd45b` |
| Backend | FastAPI + Uvicorn, GPU environment, SQLite database (`backend/sentinel.db`) |
| Frontend | Next.js 16 production build at `http://localhost:3000` |
| How it was recorded | Playwright drove a real Chromium browser through the UI and recorded the session; converted to MP4 with ffmpeg |

## Camera / video source

The Sentinel Grid cameras were mostly disconnected or degraded at recording time (at 0:11 the registry shows 5 connected, 7 reconnecting, 18 disconnected; see Limitations). So the demo camera is a **recorded clip of real grid footage**: a 16-second night-time clip from GRID-cam02 (Janpath junction, 1920×1080, about 2.8 fps). It was uploaded through **Cameras → Add Camera** as a "video file" source, which the system loops as a continuous feed. The camera was created as `DEMO-46248` ("Janpath Junction demo feed").

## Models in use

- **Detection:** Ultralytics **YOLO11s** (`yolo11s.pt`, 960 px) on the GPU. Records are stamped `yolo11s-coco-1.0`. (The brief mentions YOLOv8; the system uses YOLO11s, which replaced the earlier YOLOv8s baseline.)
- **Tracking:** ByteTrack, one tracker per camera.
- **ANPR:** YOLO plate detector + EasyOCR; enabled on the demo camera, but no plate read is shown (see below).
- **Rules:** restricted-zone rule (`rules-1.0`).

## What the video shows

| Time | Screen | Demonstrated |
|---|---|---|
| 0:00–0:05 | Title card | "SENTINEL VISION · Live System Demonstration" |
| 0:05–0:11 | Login → Command Center | Operator login (typed live), dashboard |
| 0:11–0:15 | Camera Management | Camera registry: 30 Sentinel Grid cameras with their live connection states |
| 0:15–0:18 | Add Camera | The recorded Janpath clip added as a new video-file camera; saving it starts the camera with AI on |
| 0:18–0:42 | Live camera page | Live annotated feed with detection overlay, AI intelligence panel, "Recent Detections" list written by the pipeline; state goes to PROCESSING |
| 0:42–0:50 | AI Vision (Live AI Detection) | Live stream of detections for `DEMO-46248`: car and truck rows, each with confidence and a **ByteTrack track ID** |
| 0:50–0:56 | Map → Restricted Zones | A CRITICAL restricted zone configured on the demo camera through the form |
| 0:56–0:58 | Live camera page | Rules engine evaluating live detections |
| 0:58–1:02 | Alert Center | **Real CRITICAL zone-entry alerts** for `DEMO-46248`, raised about 2 s after the zone was saved (first: `alt_a6e1a663a7`) |
| 1:02–1:07 | Alert detail | What happened, where, when, confidence, reasons |
| 1:07–1:12 | Incident detail | Incident `inc_c31428b617`, opened automatically by the CRITICAL alert |
| 1:12–1:18 | Evidence detail | Evidence `evd_2a1c4c3648` (event clip of the alert) with SHA-256 recorded at capture; integrity status shown as **NOT YET VERIFIED** (the verify check was not run in the video) |
| 1:18–1:23 | Camera Control Center | `DEMO-46248` online with **AI RUNNING**; grid cameras honestly shown as degraded |
| 1:23–1:29 | Command Center | Live AI Activity updating with **PERSON, CAR and TRUCK** detections from `DEMO-46248` |
| 1:29–1:32 | End card | "SENTINEL VISION · Live Detection Demonstration" |

Short caption labels at the bottom of the screen name each step. They were added by the recording script, not by the application.

## Not demonstrated in this video

- **ANPR / EasyOCR plate reading.** ANPR ran on the demo camera, but the night-time clip gave no readable plate on screen, so no plate read is claimed.
- **Evidence integrity verification and the evidence package export.** The evidence record and its SHA-256 are shown; the VERIFY button and the incident evidence package (JSON/PDF) were not used in the final take.
- **Live Sentinel Grid streaming with AI.** Only a few grid cameras were connected, and none is shown with live detections.
- **Camera map.** The demo camera has no coordinates, so the map was not part of this take.
- **Self-Heal screens**, beyond the degraded grid states visible in the registry and Camera Control.

## Limitations observed during recording

- **Grid:** most grid cameras were disconnected or degraded (partial authentication on the grid side), so a recorded clip was used as the reliable source.
- **Sparse boxes on the live video:** the source clip is only about 2.8 fps, and the overlay is drawn from the most recent inference, so boxes appear intermittently. The detections list and the AI Vision feed show them continuously.
- **Timestamps:** some screens show event times in UTC (for example "8:21 PM") while the dashboard clock shows local time (01:52 IST).
- **Caption correction:** in the final take, the caption during the evidence step (1:12–1:18) originally read "integrity verified", which was wrong because the page shows NOT YET VERIFIED. That caption strip was covered with the corrected text "Evidence captured automatically for the alert (SHA-256 recorded at capture)". Nothing else in the frame was changed.
- **A second take was discarded.** Its new camera stayed disconnected for about 40 s before connecting, which made the video 2 min 40 s long with a dead feed.

## Records created by the recording

These are real rows created by the application during recording. They remain in the database as the audit trail of the demo.

- Cameras `DEMO-46248` (final take) and `DEMO-54660` (discarded take), both **retired after recording** so they stop raising alerts.
- One restricted zone per demo camera ("Janpath junction restricted area").
- The zone-entry alerts, incidents (`inc_c31428b617` and one from the discarded take) and evidence clips they produced.
- Audit-log entries for the logins, camera creation, zone creation and retirements.
