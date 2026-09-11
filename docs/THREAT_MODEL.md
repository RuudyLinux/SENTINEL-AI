# SENTINEL VISION — threat model

10/10 roadmap P15. Every row below links a threat to a control that actually
exists in this codebase (file:line, not prose) and a test that actually
exercises it — no generic security writing. **Result** reflects tests run
against this exact codebase on 2026-09-10, not a static claim: `59 passed, 0
failed` across every test cited below (`pytest tests/test_route_authorization.py
tests/test_evidence_integrity.py tests/test_evidence_security.py
tests/test_upload_validation.py tests/test_rbac_enforcement.py
tests/test_ws_auth.py tests/test_audit_chain.py tests/test_camera_control.py
tests/test_sentinel_grid.py -q`).

| Threat | Control | Test | Result |
|---|---|---|---|
| **Camera SSRF** — an operator-supplied camera source URI used to probe/reach internal hosts the backend can see but the caller cannot | `POST /api/cameras/test-connection` requires `Administrator`/`Control Room Operator` (`app/routers/cameras.py:148-149`), closing the previously-unauthenticated probe; bounded open timeout (`settings.source_open_timeout_seconds`) prevents it tying up a worker indefinitely. An OPT-IN egress policy (`settings.camera_source_block_private_networks`, `app/pipeline/egress_policy.py`) refuses sources resolving to loopback/link-local/private/reserved addresses; it is off by default because many real deployments run cameras on exactly those ranges. **Measured**: active probing of loopback, `0.0.0.0`, link-local, IPv6 loopback, `file://` and malformed URIs returned an identical bounded failure after exactly the configured timeout, so the response discloses nothing about what is listening. **Honest limit**: with the policy off, an authorized operator can still reach an internal host, and even with it on, DNS rebinding is not prevented (see the gaps section). | `tests/test_route_authorization.py::test_test_connection_rejects_an_anonymous_caller`, `::test_test_connection_rejects_a_role_that_may_not_manage_cameras` | PASS |
| **Camera credential theft** — Sentinel Grid or RTSP credentials leaking via API response, logs, or a stored URL | `sentinel_grid_email`/`sentinel_grid_password` are `.env`-only, never hardcoded, and `CameraOut` (`app/schemas.py`) deliberately omits `source_uri` (which may embed RTSP credentials) from every API response — see the field's own comment in `schemas.py`. | `tests/test_sentinel_grid.py`, `tests/test_supervisor.py` | PASS |
| **Unauthorized streams** — viewing a live camera feed without authorization | `GET /api/streams/{camera_id}/token` requires a valid JWT (`get_current_user`) and issues a short-lived, resource-scoped token (`stream_token_ttl_seconds`) via `create_resource_token` (`app/routers/streams.py:16-23`) rather than an open stream URL. | `tests/test_ws_auth.py` (same resource-token mechanism as evidence/WS) | PASS |
| **Evidence tampering** — an evidence file altered after capture but reported as intact | Capture-time SHA-256 (`app/evidence_hash.py`), compared (never re-baselined silently) on `POST /api/evidence/{id}/verify` (`app/routers/evidence.py`) — reports `verified`/`tampered`/`unverifiable`/`no_baseline` honestly; the original digest is never overwritten by a tamper finding. | `tests/test_evidence_integrity.py` (6 cases: unmodified/modified/no-overwrite/no-baseline/missing-file/audited-as-failure) | PASS |
| **Malicious uploads** — an uploaded "video" that is actually a different/oversized/path-traversal payload | Extension allowlist (`settings.allowed_video_extensions`), size cap enforced mid-stream (`settings.max_upload_mb`, checked every chunk so an oversized upload is aborted before fully written), and the stored filename is a fresh UUID (`app/routers/cameras.py:96-144`) — the client's filename is never used as a path component. | `tests/test_upload_validation.py` (disallowed extension, size cap, UUID filename, auth-required) | PASS |
| **JWT compromise** — a forged, expired, or wrong-role token granting access | HMAC-signed JWTs (`app/security.py`), verified on every protected route via `get_current_user`/`require_roles`; a hardening-pass validator (`config.py::_enforce_production_jwt_secret`) refuses to start in production mode with the bundled dev secret or a short/placeholder one. | `tests/test_rbac_enforcement.py`, `tests/test_ws_auth.py` (invalid/missing token rejected) | PASS |
| **DB manipulation / injection** — attacker-controlled input reaching SQL | All application queries go through the SQLAlchemy ORM (parameterized by construction); the only raw `text()` calls are in `app/db.py`'s `ensure_columns`/`ensure_indexes`, which interpolate hardcoded table/column literals from `main.py` startup code, never external input (see the `# nosec B608` comments explaining exactly that at each call site). Static analysis via CodeQL runs on every push (`.github/workflows/codeql.yml`). | CodeQL (CI, static) + full backend suite (behavioral) | PASS (406 backend tests green at time of writing) |
| **Alert manipulation** — forging/altering alert status, severity, or feedback without authorization | Every alert-mutating endpoint (`acknowledge`/`escalate`/`dismiss`/`feedback` in `app/routers/alerts.py`) requires `get_current_user`; `feedback` is restricted to a validated enum (`_VALID_FEEDBACK`) server-side, not client-trusted free text, so precision metrics can't be corrupted by an arbitrary value. | `tests/test_alert_feedback.py::TestSubmitFeedback::test_unauthenticated_request_is_rejected`, `::test_invalid_feedback_value_is_rejected` | PASS |
| **Unauthorized evidence export** — downloading evidence files/packages without the right access | `/file`, `/file-token`, `/incidents/{id}/package`, `/package-token` all require auth to obtain a short-lived signed resource token before the actual file route will serve anything (`app/routers/evidence.py`); `_safe_evidence_path` additionally refuses to serve any path outside `settings.evidence_dir`, defense-in-depth against a future bad row. | `tests/test_evidence_security.py` (path traversal, outside-directory, legitimate file) | PASS |
| **WebSocket abuse** — an unauthenticated client subscribing to the live event stream, or flooding it | `ws.py` requires a valid token on connect (rejects missing/invalid before accepting); event batching is bounded (`settings.ws_batch_max_events`) so a stalled flush cannot grow the buffer without bound — oldest events are dropped and the batch honestly reports a real count rather than implying completeness. | `tests/test_ws_auth.py` (no-token / invalid-token / valid-token) | PASS |
| **Resource exhaustion** — too many concurrent operations (camera starts, uploads, bulk actions) degrading or crashing the service | Camera Control Center bulk actions run at most `MAX_CONCURRENT = 5` at a time via `asyncio.Semaphore` (`app/routers/camera_control.py:55,171`); DB pool sized to comfortably exceed the camera concurrency cap (`db_pool_size`/`db_max_overflow`, `app/config.py`); upload size capped mid-stream (see Malicious uploads above); event clips use a bounded ring buffer (`clip_pre_event_seconds`/`clip_post_event_seconds`), never an unlimited recording. | `tests/test_camera_control.py` (bulk action concurrency, partial-failure handling), `tests/test_sqlite_pool_sizing.py`, `tests/test_stress_concurrency.py` | PASS |

## Threats explicitly NOT covered here (roadmap gaps, not silent omissions)

- **DNS rebinding against the camera egress policy.** An opt-in private-range
  blocklist now exists (`settings.camera_source_block_private_networks`,
  `app/pipeline/egress_policy.py`), but it resolves the host at validation
  time while FFmpeg resolves again at connect time. A name answering publicly
  then privately defeats it. Closing that needs connect-time address pinning,
  which OpenCV's capture API exposes no hook for — so host-level egress
  firewalling remains the real control.
- **The egress policy is off by default**, because many real deployments run
  cameras on exactly the private ranges it blocks. A deployment that needs it
  must enable it; left off, camera SSRF remains role-gated only (which active
  probing showed leaks no scan information — see the SSRF row above).
- **Multi-tenant isolation** — this platform assumes one deployment serves
  one agency/tenant; there is no tenant-boundary enforcement to audit.

## How to re-verify this table

```bash
cd backend
.venv/Scripts/python.exe -m pytest tests/test_route_authorization.py \
  tests/test_evidence_integrity.py tests/test_evidence_security.py \
  tests/test_upload_validation.py tests/test_rbac_enforcement.py \
  tests/test_ws_auth.py tests/test_audit_chain.py tests/test_camera_control.py \
  tests/test_sentinel_grid.py -q
```
