# SENTINEL VISION — 30-Camera Concurrency Optimization

Real staged testing against the live Sentinel Camera Grid (`cctv.corp8.cloud`),
30 real registered cameras, AI off throughout. No fake streams, no duplicated
local streams, no manual repeated reconnects to manufacture a number.

## 1. Why bursts were worse than staggered startup

`supervisor.py._connect_eligible` ran one `for` loop that called
`worker.start_worker(camera_id)` for every eligible camera back-to-back, with
no delay between calls. `start_worker` just schedules an `asyncio.create_task`
— non-blocking — so within a few milliseconds, N cameras' workers all began
their own `cv2.VideoCapture` RTSP-TCP handshake against the external grid
**at the same instant**. That simultaneous handshake burst is what the
external grid tolerated poorly; the per-camera reconnect backoff
(2/4/8/16/30s, `worker.py._reopen_with_backoff`) only kicks in *after* a
camera's own open attempt fails — it does nothing to prevent the initial
burst itself. Local CPU/RAM were not the bottleneck (confirmed again this
pass, see §4) — the external grid's tolerance for N simultaneous new
connections was.

## 2. Fix: configurable staggered startup

New setting `sentinel_grid_stagger_seconds` (default **3.0s**, config.py).
`_connect_eligible`'s start loop now awaits this delay **between** successive
`start_worker()` calls within one sweep — never before the first, never after
the last. No hardcoded single value: it's a normal settings field, overridable
via `SENTINEL_GRID_STAGGER_SECONDS` env var like every other supervisor knob.
Applies to every automatic (re)connect path, restart included — there is no
separate "burst" code path to disable elsewhere. Manual, single-camera
operator Connect is untouched (no reason to delay one deliberate action).

## 3. Stability criteria (defined before testing, not after)

A level counts as **stable** only if, over a real ≥20s observation window:
- All intended cameras for that level reached `CONNECTED`
- No duplicate workers (`RUNNING` dict keyed by camera_id — structurally one
  worker per id; verified by unique `grid_state`s per camera code)
- `last_frame_at` fresh (seconds old, not stale) for every connected camera
- `reconnect_count` bounded, not climbing (no runaway reconnect loop)
- Zero `AUTH_ERROR`
- Zero AI processing (`ai_person`/`ai_vehicle` False for every camera)

## 4. Staged results (real, measured, this session)

| Level | Startup method | Connected | Stable? | CPU (backend proc) | RSS | System RAM free |
|---|---|---|---|---|---|---|
| 5 | staggered (3s, default) | **5/5** | **Yes** | 104% | 1136 MB | 3.49 GB |
| 5 | burst (0s, comparison) | 2/5 | No | — | — | — |
| 8 | staggered (3s, default) | 3/8 | No | 54% | 825 MB | 3.90 GB |
| 8 | staggered (6s, retuned) | 7/8 | Close, not full | 94% | 1651 MB | 3.21 GB |

Staging stopped at level 8 per the task's own rule ("only proceed if the
previous level is stable" / "stop rather than push merely to reach a number")
— level 8 did not reach the strict "all intended cameras connected" bar at
either stagger value tested, so 10/15/20/25/30 were **not attempted**.

## 5. Burst vs staggered — the actual comparison

Same level (5), same code, same external grid, only `sentinel_grid_stagger_seconds`
changed:

| Method | Connected | Failure rate |
|---|---|---|
| Burst (0s) | 2/5 | 60% |
| Staggered (3s) | 5/5 | 0% |

This is the core finding: staggering measurably and substantially improves
real connection success at a level (5) that both configurations were asked
to reach. It did **not** fully solve level 8 — 3s staggering left it at 3/8;
6s staggering improved it to 7/8, still short of "all connected."

## 6. Local machine headroom

RSS grew from 1136 MB (5 connected) to 1651 MB (7 connected) — roughly
150-200 MB/camera beyond the ~450 MB fixed process overhead (torch/ultralytics/
easyocr imports), consistent with earlier measurements. Extrapolating
linearly, 30 concurrent decodes would need roughly 5-6.5 GB RSS — on this
16.9 GB machine (~4.7 GB free at idle baseline, already shared with an IDE and
other tooling), that is a real, independent reason (on top of the external
grid's own tolerance) not to push toward 30 without dedicated headroom. CPU
stayed well within budget at every level actually reached (≤105% of one
logical core out of 16).

## 7. AI isolation — confirmed

At every stage: `ai_person`/`ai_vehicle` False for all connected cameras,
`grid_state` never `PROCESSING`. Zero AI workers auto-started. (Separately,
the previous UI pass's real single-camera test already verified one AI
worker starts and runs correctly on demand — not re-run here to avoid
redundant load on the grid.)

## 8. Reconnect / duplicate-worker behavior

Transitions `CONNECTED → RECONNECTING → CONNECTED` and
`CONNECTED → RECONNECTING → DISCONNECTED` both observed for real during
staging, consistent with the existing 1/2/4/8/16s backoff (see §8b — this
figure was corrected after this section was first written). No duplicate
workers at any point (dict-keyed by camera_id, and the existing
`test_connect_eligible_never_starts_a_second_worker_for_an_already_running_camera`
/ `test_sweep_does_not_undo_a_manual_disconnect` tests cover this
structurally). Not intentionally attacked/overloaded — each stage was one
clean staggered rollout, observed, then cleanly stopped before the next.

## 8b. Addendum — re-verification with a longer observation window

Follow-up request: push past 5 with careful tuning rather than a blanket
cap change. Two corrections to the record above:

- **Backoff schedule correction**: `reconnect_backoff_base=1.0`,
  doubling, capped at 30s, over `reconnect_max_attempts=5` is actually
  **1/2/4/8/16s** (not 2/4/8/16/30 as stated earlier in this doc) — the
  earlier figure was never numerically checked. Total internal retry
  budget per connection attempt is ~31s, not ~60s.
- **Level 8 re-tested with a 5-minute window** (previous runs were judged
  after only ~60-80s, before every staggered camera's own retry budget
  could have played out — camera #8 with 6s stagger doesn't even start
  trying until t≈42s). Result: it does **not** converge given time — it
  settles into an oscillating equilibrium, mostly 4/8, briefly touching 3
  or transient RECONNECTING flurries, never durably higher. This is a
  stronger, more honest result than the earlier "not yet stable" — it's
  genuinely not stable at any observed duration, not just "needs more
  patience."
- **Level 5 re-verified over 3 minutes the same session**: took longer to
  fully settle than the original test (~140s vs ~40s — the grid was
  measurably noisier this run), but **did reach and hold 5/5** for the
  remainder of the window. 5 remains the one number that has now been
  independently reproduced stable on two separate days/sessions.
- Local RAM/CPU stayed uninvolved in the level-8 failure (884 MB RSS,
  4.17 GB system RAM still free at the end of that 5-minute run, most
  workers dead) — confirms again this is the external grid's own
  tolerance, not this machine.

No config change made as a result: the default cap was already 5, which
is the only number with two independent stable confirmations. Default
`sentinel_grid_stagger_seconds` stays 3.0s — 6s helped a single run at
level 8 but didn't produce a durable result either.

## 9. Tests added (108 total, up from 105)

`test_connect_eligible_staggers_successive_starts`,
`test_connect_eligible_no_stagger_after_the_last_camera_started`,
`test_stop_supervisor_cleans_up_while_a_sweep_is_mid_stagger` — staggering
itself, configurability, and shutdown mid-stagger. Existing cap, duplicate-
prevention, reconnect, backoff, operator-disconnect, AI-off, and real/
simulated-separation tests already covered those areas and needed no
change (stagger defaults to 0 in the test fixture so they stay fast).
108/108 passed, 3 clean reruns. TypeScript and frontend build unaffected
(backend-only change) — reconfirmed clean.

## 10. Verdict

**Maximum stable concurrent real Sentinel Grid connections verified this
session: 5/5**, via staggered startup — 0 failures, 0 reconnects needed,
confirmed against burst (2/5) at the same level. **8 was attempted, not
verified stable** — best result 7/8 with a longer (6s) stagger. **15, 20,
25, and 30 were not attempted** — no basis to skip ahead past an unstable
intermediate level. This does not change the existing architecture claims:
30 real cameras remain registered; the 80,000-camera figure remains a
separate logical-scale architecture target, never real concurrent
connections.

**Never claim**: "30 cameras are live 24/7." **Accurate claim**: 30 real
Sentinel Grid cameras registered; 5 concurrent real connections verified
stable via staggered startup; 8 attempted, not fully stable; higher levels
not attempted this session.
