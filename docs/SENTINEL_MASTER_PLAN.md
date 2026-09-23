# SENTINEL VISION — master remediation plan

One consolidated plan covering every known problem in the system, prioritized by
risk × impact, with each item marked **ACTIONABLE** or **BLOCKED** and the
blocker named.

## Read this first

**"Perfect" is not a deliverable, and this plan does not promise it.** What it
promises is that every known defect and limitation is listed here, with an
owner-shaped next action, and that nothing known-broken is hidden.

**The single highest-impact problem cannot be solved by engineering effort.**
ANPR accuracy is 0.24 exact match. The bottleneck is measured and understood
(character recognition), the fix is designed and specified (a plate-trained
recogniser), the tooling is built and tested (1069 tests), and the work is
blocked on **two authorizations only you can obtain**:

1. A lawfully usable training corpus.
2. Written permission to collect footage from deployment cameras.

No amount of continued work moves that. Everything else in this plan is ordered
so that the blocked work is ready to start the day those arrive, and so that the
actionable work is not waiting on them.

---

## Problem register

Severity: **P0** operational/legal risk · **P1** core capability · **P2**
robustness/quality · **P3** hygiene.

| # | Problem | Sev | Status | Evidence |
|---|---|---|---|---|
| 1 | ANPR exact match 0.24 | P1 | **BLOCKED** — data rights + authorization | `ANPR_ACCURACY.md` |
| 2 | 55% of accepted plate reads are wrong (FP-among-accepted 0.5455) | **P0** | **PARTIALLY ACTIONABLE** | Phase 1 measurement |
| 3 | Ultralytics AGPL-3.0 in a police deployment | **P0** | **ACTIONABLE** (legal) | `ultralytics.com/license` |
| 4 | No encryption at rest for evidence/dataset | **P0** | **ACTIONABLE** | `PRIVACY_GOVERNANCE.md` |
| 5 | No legal-hold: evidence for an open incident can be purged | **P0** | **ACTIONABLE** | `PRIVACY_GOVERNANCE.md` |
| 6 | Docker deployment never built or run | P1 | **ACTIONABLE** | `README.md` |
| 7 | Plate detector mean IoU 0.136 | P2 | **ACTIONABLE** | Phase 2 |
| 8 | Temporal consensus built but never measured | P1 | **BLOCKED** — needs video | Phase 1/2 |
| 9 | CCTV plate-size distribution unknown | P1 | **BLOCKED** — needs footage | M1.5 |
| 10 | No `INSUFFICIENT_RESOLUTION` state | P2 | **ACTIONABLE** | M1.5 §11 |
| 11 | SQLite UNIQUE gap on in-place upgrades | P2 | **ACTIONABLE** | `models.py::Vehicle` |
| 12 | DNS rebinding defeats egress policy | P2 | **ACTIONABLE** (infra) | `THREAT_MODEL.md` |
| 13 | No data-subject access/erasure | P2 | **ACTIONABLE** | `PRIVACY_GOVERNANCE.md` |
| 14 | No image redaction in evidence exports | P2 | **ACTIONABLE** | `PRIVACY_GOVERNANCE.md` |
| 15 | Demo asset contains no vehicles; benchmark numbers reflect a floor workload | P3 | **ACTIONABLE** | `ANPR_ACCURACY.md` |
| 16 | No multi-tenant isolation | P3 | Accepted (single-agency design) | `THREAT_MODEL.md` |

---

## Why #2 outranks #1

ANPR accuracy is the famous number. **False positives are the dangerous one.**

At 0.24 exact match and 0.5455 FP-among-accepted: when the system asserts it has
read a plate, **it is wrong more often than it is right**. In a police context
that is not a quality issue, it is a wrongful-stop and wrongful-investigation
risk. A watchlist alert naming a vehicle that was never there is worse than no
alert.

Accuracy is blocked. **Precision is not** — but A1 measured which levers
actually work, and the obvious one does not:

| lever | verdict |
|---|---|
| Raise `plate_min_confidence` | **REFUTED.** No threshold reaches precision > 0.5; the correct and wrong confidence distributions overlap almost entirely. Raising it only trades recall away. Not changed. |
| Require corroboration for CRITICAL escalation | **SHIPPED** (A1). The only signal measured to separate correct from wrong reads. |
| Default `PLATE_REQUIRE_CONSENSUS=true` | **OPEN** — a recall/precision trade that should be decided once the CCTV size distribution is known, since it interacts with how many frames a vehicle is actually visible for. |
| Surface "uncorroborated" more prominently in the operator UI | **OPEN**, cheap |

**The remaining choice is a policy decision, not a technical one**, and it is
yours: should the system report fewer plates more reliably, or more plates less
reliably? It is currently tuned toward the latter. A1 removed the most dangerous
consequence of that (auto-escalation on one frame), but the underlying operating
point is still undocumented as a deliberate choice.

---

## Phase A — Do now (no authorization required)

Ordered by risk reduction per unit of effort.

### A1. Precision hardening *(P0)* — **DONE 2026-09-12, premise refuted**

Swept. **The premise was wrong, and the measurement says so** — see
`docs/ANPR_ACCURACY.md`, "A1".

No confidence threshold reaches precision above 0.5. Correct reads span
0.262-0.990 and wrong plate-shaped reads span 0.260-0.956, with six of seven
wrong reads at or above the lowest correct read. The distributions overlap, so a
threshold cannot separate them — raising the floor only trades recall away.
**`plate_min_confidence` was therefore NOT changed.**

The sweep did surface a real defect: `UP84AE9889` read as `UP81AE9889` at 0.956
confidence would, under the confidence-only watchlist gate, raise a CRITICAL
alert and auto-open an incident about a vehicle that was never there.

*Shipped instead:* CRITICAL watchlist escalation now requires a confident read
**AND** corroboration across frames (`Vehicle.plate_corroborated`,
`WATCHLIST_REQUIRE_CORROBORATION`, default on). Uncorroborated matches still
fire at HIGH, labelled. 857 tests pass; 6 new regression tests.

*Not claimed:* any accuracy improvement. Exact match stays 0.24. The real-world
precision effect is unmeasured — this corpus has no tracks, so corroboration
cannot fire on it.

### A2. Licensing review *(P0, external)*

Ultralytics YOLOv8 is AGPL-3.0. Compliance means publishing the complete
corresponding source of the entire derivative work; an Enterprise Licence
removes that. Decide: publish, buy, or replace the detector.

This affects the **currently deployed** system, not future work. It is the
cheapest P0 to resolve and the most expensive to discover late.

### A3. Legal hold *(P0, ~3 days)*

`POST /api/governance/purge-expired` can delete evidence attached to an open
incident. Add a hold: refuse to purge evidence linked to an incident that is not
closed, overridable only by an explicit, audited administrator action.

*Exit:* purge refuses held evidence; regression test; `PRIVACY_GOVERNANCE.md`
updated.

### A4. Encryption at rest *(P0, ~2 days + infra)*

Not currently provided. Most cheaply solved at the volume/disk layer rather than
in the application. Document the requirement, provide compose/deployment
guidance, and state plainly what the application does and does not guarantee.

### A5. Build and run the Docker deployment *(P1, ~2 days)*

The images have never been built or run. `test_deployment_contract.py` verifies
the *contract* between the compose files and the app — that is drift protection,
not proof it boots. Until someone runs `docker compose up --build`, the
production deployment path is unverified.

*Exit:* a real run, a recorded result, README corrected either way.

### A6. `INSUFFICIENT_RESOLUTION` state *(P2, ~3 days)*

The pipeline currently has no way to say "this plate cannot be read". It either
produces a string or produces nothing, and "nothing" is indistinguishable from
"not attempted". Add an explicit state, surfaced in the UI and stored on the
sighting.

Worth doing *before* the CCTV measurement, because if that measurement returns
Scenario C (most plates <20px) this becomes the primary deliverable rather than
a nicety.

### A7. Plate detector improvement *(P2, ~1 week)*

Mean IoU 0.136; 12 of 16 detections land on non-plates. Phase 2 proved this is
**not an accuracy lever** (perfect localization changed exact match by 0.00), so
it must not be funded as one. It is worth doing for:
- evidence quality — `plate_bbox` is what an operator reviews;
- cost — a correct tight crop is ~3× cheaper to OCR;
- and it becomes an accuracy lever *later*, once a better recogniser exists.

### A8. Hygiene *(P3, ~2 days)*

SQLite UNIQUE gap on in-place upgrades (#11) · document the DNS-rebinding
mitigation as host-level firewalling (#12) · replace or remove the misleading
demo asset and re-baseline the camera benchmark (#15).

---

## Phase B — Blocked on authorization

Nothing here starts until M0 clears. All of it is specified and tooled.

| step | what | ready? |
|---|---|---|
| B1 | Data-use agreement + corpus licensing | **the blocker** |
| B2 | Unlabelled footage → CCTV size measurement (M1.5) | tool built, tested |
| B3 | Pilot corpus: 2,000 vehicles, ≥500 vehicle-disjoint test | spec frozen, QC tooling built |
| B4 | CRNN + CTC training | architecture chosen, justified |
| B5 | PARSeq ceiling comparator | same data, same splits |
| B6 | Synthetic rare-glyph ablation | `I O Q V Z` have zero coverage today |
| B7 | **Decision gate: ≥0.319 on 500 plates** | statistically derived |
| B8 | Video benchmark → first real temporal-fusion measurement | metric defined |
| B9 | Integrate behind `ANPR_ENGINE`, default off | interface designed |

**B2 is the cheap unlock.** Unlabelled footage needs far less authorization than
a labelled corpus, and it answers a question that changes B4's architecture:
if most plates are under 20px, a 32px-input CRNN is the wrong design and we
would rather know before annotating 2,000 plates.

---

## What "solved" means, per problem

Honest end-states, because several of these do not end in "fixed":

| problem | realistic end-state |
|---|---|
| ANPR accuracy | **Measurably better than 0.24 on a vehicle-disjoint CCTV test set.** Not a promised number. It may land at 0.5; it may land at 0.35. The gate is statistical significance, not a target |
| False positives | A *chosen*, documented operating point — not zero |
| AGPL | A decision recorded: publish, buy, or replace |
| Legal hold / encryption / erasure | Mechanism provided; policy remains the agency's |
| Detector IoU | Materially better, justified on cost and evidence quality, **not claimed as accuracy** |
| Temporal fusion | **Measured** — it may turn out not to help |
| CCTV size | Measured, with the architecture decision it implies |
| Deployment | Actually run, once |

**Two of these may end in "this does not work well enough".** Scenario C (plates
too small to read) and a failed B7 gate are both legitimate outcomes, and the
plan treats them as results rather than as failures to be worked around. A
system that says `INSUFFICIENT_RESOLUTION` honestly is worth more to an
investigation than one that invents plates.

---

## What I recommend you do next

**One thing, this week:** start A2 (AGPL review) and B1 (data authorization) in
parallel. Both are external, both are on critical paths, and both get slower the
longer they wait.

**One thing I can start immediately on your word:** A1 — precision hardening. It
is the largest risk reduction available without any new data, it needs no
authorization, and it directly addresses the problem that the system currently
asserts wrong plates more often than right ones.

I have not started it, because every prior phase has been gated on your explicit
approval and this one changes production behaviour.
