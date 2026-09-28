# M1 — ANPR dataset tooling

**Status: PASS.** The dataset-management, split, QC and evaluation tooling is
built and tested against the frozen Phase 3 / M0 specification.

**No real plate data was introduced, no model was trained, and no production
ANPR code was modified.** Every test runs on synthetic fixtures, with no GPU, no
OCR engine, no camera, no network and no external dataset.

M1 deliberately proceeds while **M0 remains BLOCKED**: when authorization
arrives, annotation begins against tooling that is already tested, rather than
tooling written in a hurry around whatever data turns up.

---

## 1. Dataset schema

`training/schema.py` — the canonical record, one per annotated **plate
observation** (not per frame, not per vehicle), serialized as JSON Lines.

```json
{
  "image_id": "C-001_vehicle_0042_f003",
  "camera_id": "C-001",
  "timestamp": "2026-09-12T08:14:13+05:30",
  "vehicle_id": "vehicle_0042",
  "plate_bbox": [100.0, 200.0, 320.0, 260.0],
  "plate_text": "GJ05AB1234",
  "quality": "clear",
  "visibility": "full",
  "label_confidence": "certain",
  "split": "train"
}
```

Optional fields — `plate_quad`, `image_width`/`image_height`, `glyph_px`,
`vehicle_category`, `plate_style`, `plate_face`, `plate_row_layout`,
`conditions`, `measured` — are omitted from serialization when unset, so a
manifest never records "unknown" as though it were a measurement.

### Validation is split in two, deliberately

| layer | question | failure mode |
|---|---|---|
| **schema** (`record_from_dict`) | is this record structurally parseable? | raises `SchemaError` |
| **QC** (`qc/checks.py`) | is this record semantically sound or merely unusual? | `ERROR` / `WARNING` / `INFO` |

A malformed line never aborts a corpus load: `load_jsonl` returns
`(records, parse_errors)` so one bad line is reportable alongside everything
else, instead of hiding the other 1,999 records' problems.

### The rule this schema exists to enforce

> **Annotation validity is not vehicle-registration validity.**

`KL34F` and `KL498262` are valid **annotations** — a transcriber wrote down what
was visibly on a real plate — and invalid **registrations**. The schema accepts
both and says nothing about registration format.

This is enforced by a test that parses the module's AST and asserts it compiles
exactly one regex, the character policy `^[A-Z0-9]*$`. Adding a registration
check to the schema would delete exactly the non-standard plates Phase 2
measured as **5 of 10 recognition failures**, and would restrict the corpus to
plates the system already reads — making every subsequent accuracy figure
circular.

### Privacy in the schema

`vehicle_id` is an opaque dataset-local id, never the registration and never an
operational track id. `camera_id` is an opaque deployment code, never a URI.
The registration appears exactly once, in `plate_text`. A test asserts the
record type has no `source_uri` / `rtsp_url` / `password` / `camera_url` field,
so camera credentials cannot reach a manifest.

---

## 2. Split strategy

`training/split.py`. The rule:

```
ONE vehicle identity  ->  ONE split. Always.
```

### Assignment is by stable hash, not by shuffling

`sha256(f"{seed}:{identity}")` mapped onto the split ratios. Two properties
follow, both load-bearing:

**Order independence.** The result cannot depend on dict/set iteration order or
on the order records appear in the file. `hashlib` rather than the builtin
`hash()`, which is salted per process and would produce a different split on
every run.

**Growth stability.** Adding 500 vehicles next month leaves every existing
vehicle in the split it was already in. With shuffling, appending one record
reshuffles everything — the frozen test set silently changes and results
measured before and after stop being comparable, while still looking like they
are.

The trade is that ratios are approximate on small corpora: identities are hashed
independently, so a 70/15/15 request over 20 vehicles will not land on 14/3/3.
`plan_split` reports achieved counts so the deviation is visible.

### Unassigned records

A record with an empty identity is left at `split=""` rather than defaulted into
train. An observation whose vehicle is unknown cannot be guaranteed disjoint
from anything, so it must never silently become training data. QC reports them.

---

## 3. Leakage policy

Three distinct checks, graded differently because the fixes differ:

| check | severity | rationale |
|---|---|---|
| **identity leakage** — one `vehicle_id` in >1 split | **ERROR** | fatal. Frames of one vehicle are near-duplicates; the model is scored on its own training data |
| **image leakage** — one `image_id` in >1 split | **ERROR** | the same *pixels* in two splits. Happens legitimately when one frame holds two vehicles that hash into different splits |
| **repeated plate text** — one registration under >1 `vehicle_id` | **WARNING** | usually one vehicle given two ids; sometimes a genuine coincidence (`KL34F`) or a cloned plate. A human decides |

Repeated plate text is **reported, never auto-failed**. Auto-failing would delete
genuine data; ignoring it would hide a real identity bug.

### Near-duplicate detection

At manifest level, near-duplicates are approximated by *identical geometry for
the same vehicle on the same camera* — the same box, repeatedly, is almost
always consecutive frames of a stationary vehicle. Reported as
`suspicious_duplicate_frames` (WARNING) so one parked car cannot dominate
training. True perceptual hashing belongs at crop-export time, where the images
exist; this is the tripwire that works without them.

### Test-set freeze

Once benchmarking starts the test split is frozen. A relabelling that fixes a
genuine annotation error requires a **version bump** (`v1` → `v2`) and re-running
every prior configuration — a number from `v1` and a number from `v2` are not
comparable.

---

## 4. QC checks

`training/qc/checks.py`, three severities:

| severity | meaning | effect |
|---|---|---|
| `ERROR` | structurally invalid or fatal to a valid experiment | blocks the dataset |
| `WARNING` | suspicious; a human must look | flagged for review |
| `INFO` | notable, not a defect | recorded |

**ERROR:** `missing_image_id`, `missing_vehicle_id`, `invalid_timestamp`,
`degenerate_bbox`, `negative_bbox`, `bbox_outside_image`, `illegal_characters`,
`unknown_split`, `unknown_quality`, `unknown_visibility`,
`unknown_label_confidence`, `uncertain_label_in_test`, `duplicate_record`,
`duplicate_annotation`, `cross_split_identity_leakage`,
`cross_split_image_leakage`, `empty_dataset`, `malformed_line`.

**WARNING:** `missing_camera_id`, `missing_timestamp`, `extreme_aspect`,
`tiny_plate`, `empty_plate_text`, `unusual_length`,
**`unusual_registration_format`**, `partial_plate_in_test`,
`repeated_plate_text`, `suspicious_duplicate_frames`, `unassigned_records`.

**INFO:** `unreadable_plate`, `multi_vehicle_frame`.

### Three judgements worth stating explicitly

1. **`unusual_registration_format` is a WARNING.** `QQ00QQ0000` and `KL34F` are
   flagged for review, never dropped.
2. **A deliberately unlabelled unreadable plate is INFO, not an error.** It
   trains the detector and is needed to measure honest rejection.
3. **A duplicate `image_id` is only an ERROR when it is the same vehicle.** One
   frame legitimately contains several vehicles; a naive duplicate check would
   reject real data.

The registration pattern used for the review flag is a deliberate **copy** of
the production one, not an import. The training tooling must not depend on the
backend, and — more importantly — tightening the production grammar for
operational reasons must not silently start failing a frozen corpus.

---

## 5. Character coverage

`qc/report.py::render_character_coverage`. Per character across `0-9 A-Z`:
occurrences, unique plates, unique vehicles, and train/val/test counts, with a
status column against the M0 targets (common ≥300 train, rare ≥100 train, all
≥20 test).

This exists because of a measured failure: the current n=25 benchmark corpus
contains **zero** instances of `I O Q V Z`, and twelve more classes appear at
most twice. That was invisible until counted, and it silently caps what any
recogniser trained on such a corpus could learn. The report prints a
`ZERO COVERAGE` banner so the same gap is obvious on day one of the next
dataset.

Unique **plates** and unique **vehicles** are counted separately from raw
occurrences, because three frames of one vehicle is one vehicle — a distinction
that otherwise inflates apparent coverage.

Rare classes are to be filled by **synthetic data**, never by duplicating real
plates: duplication teaches the model that plate rather than that glyph, and
risks a duplicate crossing a split boundary.

---

## 6. Plate-size analysis

`qc/report.py::render_size_distribution`, bucketed on **character height in
pixels** (glyph height governs legibility and is what the recogniser's input
resize is defined against):

```
<20px   20-30px   30-50px   50-75px   75-100px   >100px
```

`<20px` is separated from `20-30px` deliberately: below the recogniser's input
height a correct answer may be information-theoretically impossible, and a model
should be allowed to decline there rather than be scored as though it failed.

Buckets are a parameter (`DEFAULT_SIZE_BUCKETS`, overridable), so measured CCTV
data can move the boundaries without a code change.

Glyph height uses the annotated `glyph_px` when present, else estimates it from
the box: **55%** of box height for a single-row plate, **27.5%** for a two-row
plate, since a double-row plate stacks two character bands in the same box.
Getting that wrong would put motorcycles — the worst-case size class for CCTV —
in the wrong bucket.

Accuracy columns appear **only when predictions are supplied**. With no
predictions the report shows `—`, never a fabricated number.

---

## 7. Evaluation metrics

`training/evaluate/metrics.py`:

| metric | definition |
|---|---|
| **exact match** | case-normalized, whitespace-stripped string equality, per plate |
| **character accuracy** | `max(0, 1 − CER)`. Named separately so it is never confused with exact match |
| **CER** | `(substitutions + insertions + deletions) / ground-truth characters` |
| **edit operations** | alignment backtrace giving every `(op, truth_char, predicted_char)` |
| **Wilson interval** | 95% CI on a proportion; correct at small n, unlike the normal approximation |

CER can exceed 1.0 for an over-long prediction. That is intended: a read that
invents ten characters is worse than one that reads nothing, and capping at 1.0
would hide it.

**Insertions and deletions are reported separately, never folded into CER.**
Phase 2 found 25 substitutions but also 20 deletions and 12 insertions — 56% of
edits — which together say the recogniser is failing at *segmentation*, not at
glyph identity. A single CER number conceals that distinction entirely.

Normalization does **not** repair characters: `GJO5AB1234` stays as read. The
evaluator measures what the model produced, not a tidied version of it.

### Pinned to the backend benchmark

`TestAgreementWithTheBackendBenchmark` asserts this module's edit distance
against the implementation in `backend/tools/anpr_bench.py` — the one that
produced the 0.24 / 0.3983 baseline — over real (truth, OCR) pairs recorded in
`docs/ANPR_ACCURACY.md`. Two definitions of CER is how a before/after comparison
quietly stops being a comparison.

---

## 8. Calibration framework

`metrics.calibration(outcomes)` where `outcomes` is `(confidence, was_correct)`,
returning reliability bins (count, mean confidence, accuracy, gap) and
**expected calibration error**.

This matters more here than in a typical model report because the production
pipeline **consumes** confidence: `plate_min_confidence` gates persistence and
`plate_review_confidence_floor` decides whether a human ever sees a read. A
recogniser reporting 0.9 on reads that are right 60% of the time would defeat
both gates regardless of its headline accuracy — which is why a calibration
failure is a **blocking condition** in the Phase 3 success criteria.

**It fabricates nothing.** Empty input returns an empty report with zero bins
and zero error. No calibration number exists until real predictions do.

---

## 9. Paired model evaluation

`metrics.mcnemar(truths, predictions_a, predictions_b)` — McNemar's test on the
discordant pairs, using the **exact binomial** test when discordant pairs are
few (<25, where the chi-squared approximation is unreliable) and the
continuity-corrected chi-squared otherwise. Chi-squared survival for 1 df is
`erfc(sqrt(x/2))`, so no SciPy dependency is needed.

**It refuses to run on mismatched example sets.** Comparing a model scored on
480 samples against one scored on 500 is not a paired test, and silently
intersecting them would produce a number that looks valid and is not.

This is what enforces the Phase 3 acceptance rule: a result counts as an
improvement only with non-overlapping 95% CIs or McNemar p < 0.05. On a
500-plate test set that means **≥0.319** against the 0.24 baseline.

---

## 10. Synthetic fixtures

`training/fixtures.py`. All records are invented; plate strings use the Indian
*format* because the tooling's behaviour depends on shape, but correspond to no
real vehicle. **No images at all** — the tooling under test operates on
manifests.

Covered cases: normal plates · unusual-but-allowed text (`KL34F`, `KL498262`,
`QQ00QQ0000`, BH-series) · rare/missing characters · malformed annotations ·
invalid bboxes (degenerate, negative, outside image) · duplicate images ·
duplicate vehicles · cross-split leakage · multiple frames of one vehicle ·
multiple cameras · tiny plates · extreme aspect ratios · one record per size
bucket.

---

## 11. CI gate

A dedicated `training-tooling` job in `.github/workflows/python-package.yml` —
separate from the backend job because it shares nothing with it (stdlib only, no
torch, no OpenCV, no ffmpeg), so it runs in seconds and a failure is
unambiguous. It also runs the suite in randomized order, mirroring the backend's
inter-test coupling gate.

The job exists primarily for one assertion, `TestCiLeakageGate`:

```
vehicle_001 -> train, vehicle_001 -> test   =>  BUILD FAILS
vehicle_001 -> train, vehicle_002 -> test   =>  BUILD PASSES
```

with the error naming the identity and both splits:

```
ERROR: cross_split_identity_leakage: vehicle_id=vehicle_001 appears in: test, train
```

This is a permanent regression test. A leaked identity is undetectable from the
accuracy number itself — it simply looks like a very good model.

---

## 12. Reproducibility

A split is reproducible from `(dataset version, seed, ratios, identity field)`.
`SplitConfig` carries all four and is recorded in the split report.

Asserted by tests:

- same seed → identical assignment;
- different seed → different assignment;
- **record order does not affect assignment** (forward vs reversed input);
- **adding vehicles does not move existing ones**;
- assignment is a pure function of the identity string.

Ratios are validated to sum to 1.0 and to be non-negative.

---

## 13. Privacy and data handling

- **No real CCTV images, no real plate images, no scraping, no external personal
  data, no third-party uploads.** The tests use synthetic records only.
- `training/.gitignore` excludes `*.jsonl`, image and video extensions, and
  `data/ dataset/ crops/ frames/ splits/ reports/` — a backstop against an
  accidental `git add`, not the primary control. The primary control is that
  dataset artefacts live in the controlled environment described in
  `docs/ANPR_M0_DATA_ACQUISITION.md` §3.
- The record schema carries no URI or credential field, enforced by test.
- The tooling is stdlib-only and runs offline, so an annotation workstation
  never needs to reach the internet.
- Raw frames stay at access tier 0; annotators work at tier 1 (plate crops);
  this tooling operates at tier 2 (manifests, no imagery).

---

## 14. Transition to real authorized data

When M0 unblocks, in order:

1. **Export crops** from authorized footage at tier 0 → tier 1, stripping camera
   URIs. (`export_crops.py`, not yet written — it needs real frame layout.)
2. **Build `records.jsonl`** against `schema.py`, which already validates it.
3. **Run QC** — `run_all()` → `render_qc_report()`. Resolve every `ERROR`;
   route every `WARNING` to human review. Do not auto-drop unusual plates.
4. **Assign splits** — `assign_splits()` with a recorded seed; commit
   `splits/v1.json`.
5. **Verify leakage is empty** — the CI gate runs the same check.
6. **Generate coverage and size reports**; top up rare glyphs synthetically if
   below target; confirm the CCTV size distribution (M0 criterion 9, currently
   PARTIAL — measured only on phone photography).
7. **Freeze the test split.**
8. Only then: M2 annotation completion, and M3 training.

Nothing in steps 2–7 requires new tooling. That is the point of M1.

---

## 15. M1 exit criteria

| # | criterion | status |
|---|---|---|
| 1 | JSONL schema validates correctly | **PASS** — 43 tests |
| 2 | Deterministic splitting works | **PASS** — seed, order and growth stability asserted |
| 3 | Vehicle-disjoint splitting enforced | **PASS** |
| 4 | Cross-split identity leakage fails CI | **PASS** — `TestCiLeakageGate`, dedicated CI job |
| 5 | QC catches intentionally malformed fixtures | **PASS** — every fixture asserts its own code |
| 6 | Character coverage report works | **PASS** — reproduces the `I O Q V Z` gap |
| 7 | Plate-size report works | **PASS** — all six buckets |
| 8 | Exact match / CER / ins / del / sub metrics work | **PASS** — pinned to the backend implementation |
| 9 | Paired evaluation framework works | **PASS** — McNemar, refuses mismatched sets |
| 10 | Calibration framework exists without fabricated results | **PASS** — empty input → empty report |
| 11 | All tests pass | **PASS** — 851 backend + 163 training = **1014** |
| 12 | No real/private data introduced | **PASS** — synthetic fixtures only |
| 13 | Production ANPR code unchanged | **PASS** |
