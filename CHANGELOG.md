# Changelog

Notable changes by date. Detailed engineering notes and measurements are in
`docs/DEVELOPMENT_NOTES.md`.

## 2026-09-29

- Codebase cleanup: comments and docstrings reduced to the reasons behind the code; development
  history moved out of source files. README rewritten; the previous README is preserved as
  `docs/DEVELOPMENT_NOTES.md`.
- Fixed login stalls (18-149 s) under camera load on SQLite. Database operations now run on a
  dedicated executor, snapshot I/O happens outside write transactions, the rules engine no
  longer flushes on the event loop, and failing streams throttle error-count writes. Covered by
  `backend/tests/test_login_contention.py`.
- Sentinel Grid cameras shown on the map: 19 of 30 positioned from OpenStreetMap place names
  (`grid_locations.json`); markers for shared positions are grouped; degraded cameras drawn in
  amber, with a legend; the map fits all cameras.
- Added the challenge submission package and a live detection proof video (`Documentation/`).

## 2026-09-28

- Grid cameras stay connected with AI on by default; AI slots rotate when hardware is short.
- REC button: operator recordings of the annotated live view, stored as hashed evidence.
- One shared YOLO model with a tracker per camera; RTSP over UDP for grid cameras; latest-frame
  reader that decodes without converting when idle.
- Escalating grid circuit breaker with a single probe camera after each pause.
- Zone alerts require a minimum confidence and several frames in the zone.
- PostGIS nearby-camera search; WHEP live video; AI rate resolved from hardware.
- UI fixes: readable validation errors, Add Camera input checks, bulk progress labels, empty
  ANPR search.

## 2026-09-23 to 2026-09-24

- Plate reads made explainable (variant, agreement, corroboration); corroboration required
  before a read can drive a CRITICAL alert.
- Redis-compatible shared state for alert cooldowns, self-heal dedup and the login limiter.
- Camera lifecycle transitions validated; camera status derived from the lifecycle state.
- Thread pool sized to the number of cameras; worker split into state, connection and frame
  modules.
- `docker compose up --build` verified end to end.
- Dataset, QC and evaluation tooling for training a plate model.

## 2026-09-10 to 2026-09-12

- V2: live plate tracking, vehicle journeys, explainable risk scoring, cross-camera
  correlation, PostgreSQL support through Alembic, Docker deployment.
- Optional plate redaction for evidence packages; optional egress policy for camera sources.
- Login rate limiting keyed by username and source address.
- Fixes found by measurement and coverage work: search filters, watchlist deactivation, stream
  token expiry, list limits, zone and rule validation, incident workflow, PostgreSQL-only chart
  query, alert list robustness, SQLite migration chain.
- Frontend unit tests; deployment contract tests; coverage floor in CI.

## 2026-09-03 to 2026-09-05

- Initial platform: camera registry and adapters, YOLO detection with ByteTrack, EasyOCR ANPR,
  rules engine, alerts, incidents, evidence, dashboard.
- 24/7 grid auto-connect supervisor with staggered connects and connection-state UI.
- Self-Heal recovery log and Camera Control Center (bulk operations).
- SQLite lock retry with reapply; deterministic shutdown.
- CI: backend tests and CodeQL.
