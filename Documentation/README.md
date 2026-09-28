# SENTINEL VISION: Government Challenge Submission Package

**Unified CCTV Intelligence & Real-Time Smart Policing Platform**
Repository: <https://github.com/RuudyLinux/SENTINEL-AI> (branch `demo-readiness-phase2`, PR #2)
Package prepared: September 2026

All four documents describe the same system with the same names, numbers and limitations. Every capability was checked against the code before it was written down. Items are labelled **Current implementation**, **Demonstrated capability** or **Planned / future scale**.

## Submission checklist

| # | Submission requirement | File | Status |
|---|---|---|---|
| 1 | Solution presentation (PPTX) | `01_Solution_Presentation.pptx` (15 slides, editable, speaker note on the cover) | Ready |
| 2 | Solution presentation (PDF) | `01_Solution_Presentation.pdf` (same 15 slides, 16:9) | Ready |
| 3 | High-level design / architecture document (PDF) | `02_High_Level_Design_Architecture.pdf` (23 sections, 2 diagrams, screenshots) | Ready |
| 4 | Workflow / integration diagram (PDF) | `03_Workflow_Integration_Diagram.pdf` | Ready |
| 5 | Workflow / integration diagram (PNG) | `03_Workflow_Integration_Diagram.png` (3000 px wide) | Ready |
| 6 | Workflow / integration diagram (SVG) | `03_Workflow_Integration_Diagram.svg` (vector, editable) | Ready |
| 7 | Demonstration video script and storyboard | `04_Demo_Video_Script_Storyboard.pdf` (5-minute shot list, narration, setup, fallbacks) | Ready. **The video itself still has to be recorded** from this script. |
| 8 | Submission checklist | `README.md` (this file) | Ready |

Supporting material:

| Folder | Contents |
|---|---|
| `diagrams/` | `architecture_diagram` and `workflow_integration_diagram`, each as SVG, PNG and PDF |
| `screenshots/` | 20 screenshots of the running system (dashboard, camera map, registry, AI vision, alerts, incidents, evidence, vehicle journey, ANPR, watchlists, rules, Self-Heal, audit, users/RBAC) |
| `source/` | HTML sources of the PDFs, plus `tools/` scripts that regenerate the deck, the diagrams and the PDFs |

## Facts used across every document

Checked against the repository on 29 September 2026.

| Topic | What the documents say | Source in the repo |
|---|---|---|
| Frontend | Next.js 16, React 18, TypeScript, Tailwind, Leaflet + OpenStreetMap; 34 screens | `frontend/package.json`, `frontend/app/` |
| Backend | FastAPI; about 98 REST endpoints across 19 routers; WebSocket `/ws` with 12 event types | `backend/app/routers/`, `backend/app/ws.py` |
| Detection | Ultralytics **YOLO11s** at 960 px on one shared GPU model. YOLOv8s was the earlier baseline. | `backend/app/config.py` (`model_name`), `docs/AI_ACCURACY.md` |
| Tracking | ByteTrack, one tracker per camera | `backend/app/pipeline/detector.py`, `bytetrack_sentinel.yaml` |
| ANPR | YOLO plate detector + EasyOCR, format gate, multi-frame voting | `plate_detector.py`, `anpr.py`, `plate_tracker.py` |
| Camera sources | Webcam, video file, RTSP (TCP/UDP), Sentinel Grid, mock VMS; ONVIF is a stub | `backend/app/pipeline/adapters.py` |
| Database | **SQLite (WAL) is the demo database.** PostgreSQL 16 + PostGIS is implemented as the Docker Compose deployment path; CI builds it and runs migrations. It has not been load-tested with a live camera fleet. | `docker-compose.yml`, `backend/alembic/`, `.github/workflows/docker.yml` |
| Security | bcrypt, JWT, 5 roles with per-route RBAC, stream tokens (1 h), evidence tokens (5 min), hash-chained audit log | `security.py`, `audit.py`, `config.py` |
| Evidence | Snapshot per alert, event clips, REC recordings, SHA-256 at capture, incident evidence package (JSON/PDF, optional redaction) | `routers/evidence.py`, `pipeline/clips.py`, `pipeline/recorder.py` |
| Map | 19 of 30 grid cameras placed from OpenStreetMap; 11 intentionally not mapped; grouped markers (3 in Bilimora); amber = degraded; legend; vehicle journey map, GRID-cam06 verified at Timbavadi, Junagadh | `pipeline/grid_locations.json`, `components/CameraMap.tsx` (PR #2, commit `607fc72`) |
| Tests | Backend 1096 passed after the map change (`607fc72`); **1101 passed, 18 skipped** after the contention fix (`795ebc8`). Frontend: 23 unit tests, 10 Playwright browser tests. CI (backend, frontend, Docker, CodeQL) green on PR #2. | `backend/tests/`, `frontend/lib/*.test.ts`, `frontend/e2e/` |

## Known limitations, disclosed in every document

1. **Grid streaming is partial.** Grid authentication partly works; in recent runs about 29 cameras were degraded while a few connected, with about 3.4 Mbit/s of total video. At other times the grid rejected the account (HTTP 401/403). The package never claims 30 simultaneous grid streams.
2. **SQLite is single-writer.** Logins were observed taking 18 to 149 s with grid cameras connected. The root cause was measured and fixed in commit `795ebc8`, with regression tests; afterwards login measured 0.25 s median with 8 cameras. The fix has **not yet been re-verified with all 30 grid cameras streaming**, because grid access was blocked at the time. PostgreSQL is recommended for production.
3. **Map coverage.** 11 of 30 grid cameras have no reliable location and are not mapped; 4 of the 19 are at town level only.
4. **Single process, single machine.** All camera workers run in one backend process; AI throughput is bounded by that machine's GPU.
5. **Accuracy figures are internal.** Detection figures come from a small internal benchmark whose reference labels were reviewed by an AI assistant. They are quoted only with that caveat, in the architecture document.

## Where the repository differs from the original brief

These were resolved in favour of the code, as the honesty rule requires:

- The brief names **YOLOv8**. The code runs **YOLO11s**; YOLOv8s was the earlier baseline and was replaced after benchmarking. All documents say YOLO11s.
- The brief says PostgreSQL may not be implemented. **The Docker Compose stack runs PostgreSQL + PostGIS**, and CI builds and migrates it. The documents present SQLite as the demo database and PostgreSQL as the implemented deployment path, not yet load-tested.
- The brief says the login contention is unfixed. **A fix was committed (`795ebc8`) after the brief was written.** The documents say it is fixed and tested, but not re-verified with 30 live grid streams.
- Kubernetes, Kafka and cloud deployment are **not** implemented. They appear only as generic future items ("container orchestration", "event streaming", "multi-node"), clearly labelled future.

## Before you submit

- [ ] Record the demonstration video from `04_Demo_Video_Script_Storyboard.pdf` (setup takes about 15 minutes).
- [ ] If the challenge has its own slide or page limit, trim the deck. Slides 12 to 14 are the easiest to merge.
- [ ] If the grid status or login verification changes before submission, update Limitations 1 and 2 in all four documents (`source/` holds the editable HTML; `source/tools/deck.py` rebuilds the PPTX and PDF).
- [ ] Open the PPTX once in PowerPoint to check fonts (Segoe UI is used; PowerPoint substitutes it where missing).

## Regenerating the package

From the repository root, with the backend and frontend running for screenshots:

```
node Documentation/source/tools/shots.cjs                                   # screenshots (run from frontend/)
python Documentation/source/tools/diagrams.py Documentation/diagrams         # diagrams (SVG)
node Documentation/source/tools/svg2png.cjs <svg files>                      # diagram PNG + PDF
python Documentation/source/tools/deck.py Documentation                      # PPTX + presentation HTML (needs python-pptx, pillow)
node Documentation/source/tools/html2pdf.cjs <html> <pdf> [slides|doc] [footer]  # PDFs
```
