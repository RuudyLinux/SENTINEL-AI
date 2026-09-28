# M1.5 — CCTV plate-size analysis

**Status: TOOLING READY — DATA COLLECTION BLOCKED.**

The measurement tooling is built and tested. **No CCTV footage was accessed, no
deployment camera was touched, and no real plate data was used.** M0 remains
blocked, and this phase deliberately does not work around that: it builds the
instrument so the measurement can run the same day authorization arrives.

**No CCTV statistics appear in this document**, because none have been measured.
Any number below is either from the existing phone-photo benchmark (labelled as
such) or from a synthetic fixture used to test the tooling.

---

## 1. Why the phone-photo measurement is insufficient

Every ANPR number this project has is derived from one corpus of mobile-phone
photographs. Its measured glyph-height distribution:

```
min 12.7   p25 41.2   median 75.4   p75 119.9   max 595.1  px
```

11 of 25 samples exceed 100px glyphs. A CCTV camera covering a junction will
rarely produce that: the plate is further away, the sensor is shared across a
wide field of view, and the image is H.264-compressed before anything sees it.

This matters concretely, in three places:

1. **Architecture.** The planned CRNN input is 32px tall. If the deployment's
   plates carry 20px glyphs, that input is being asked to resolve characters
   from fewer pixels than it has rows.
2. **Phase 2's conclusion has a stated boundary.** It found that resolution was
   *not* the bottleneck — but it found that on a corpus with 75px median glyphs.
   That finding explicitly does not transfer to CCTV, and this measurement is
   what would tell us whether it holds there.
3. **The honest-failure requirement.** If a material share of plates is
   genuinely unreadable, the correct engineering answer is an explicit
   `INSUFFICIENT_RESOLUTION` state, not a better decoder.

---

## 2. Methodology

Two components, deliberately separated:

| module | role | dependencies |
|---|---|---|
| `training/sizing.py` | statistics core: observations, buckets, percentiles, sampling, reporting | **stdlib only** |
| `training/analyze_cctv_size.py` | CLI: video/image decoding, plate detection, manifest reading | OpenCV + the backend detector, **imported lazily** |

The split is what lets the entire measurement be verified on synthetic
observations before any footage exists — and it means an annotation workstation
never needs torch installed to run the statistics.

### What is measured, per plate observation

frame resolution · plate box width/height · aspect ratio · estimated character
height · plate area as a fraction of the frame · plate area as a fraction of its
vehicle crop (when a vehicle box is supplied) · camera · track · time of day ·
vehicle category · plate face · row layout.

**Every plate in a frame is measured, not just the best one.** A frame
legitimately contains several vehicles, and measuring only the top-scoring box
would bias the distribution toward whichever plate is largest or most central.

### What is NOT measured

**No OCR. No plate text is read, stored, printed or logged.** The question is
how many pixels a plate has, and answering it does not require knowing which
vehicle it is. That keeps this tool usable at a lower privacy tier than
annotation.

---

## 3. Box provenance — detector-estimated vs ground truth

This distinction is enforced in the data model (`PlateObservation.source`) and
stated at the top of every report.

| source | meaning | trust |
|---|---|---|
| `ground_truth` | human-drawn boxes from a JSONL manifest | measurement |
| `detector` | boxes from `plate_detector.detect_plates` | **estimate only** |

On the labelled benchmark the detector reached **mean IoU 0.136**, and **12 of
its 16 detections landed on something that was not a plate**. A size
distribution built from those boxes describes what the detector found, which is
not the same as what is there.

So a detector-sourced report carries this banner, and the word "ground truth"
never appears in it:

> These sizes come from the plate detector, **not** from human annotation. On
> the labelled benchmark that detector reached mean IoU 0.136… Treat every
> number below as a *detector-estimated* distribution.

**Calibration path:** `--annotations manual_sample.jsonl` measures human-drawn
boxes instead. Boxing 100–200 plates by hand from the same footage and comparing
the two distributions is the cheapest way to learn whether the detector's
estimate is usable at all — and it is strongly recommended before any
architecture decision rests on these numbers.

---

## 4. Plate-size buckets

Unchanged from M1, keyed on **character height**, not plate width:

```
<20px   20-30px   30-50px   50-75px   75-100px   >100px
```

`<20px` is separated from `20-30px` because below the recogniser's input height
a correct answer may be information-theoretically impossible. **Sub-20px plates
are counted and reported, never discarded** — they are the operational case for
an explicit `INSUFFICIENT_RESOLUTION` state.

Per bucket the report gives: plate count, percentage, per camera, per time of
day, frame resolution, plate width/height, and estimated glyph height.

Buckets are a parameter (`DEFAULT_SIZE_BUCKETS`), so measured data can move the
boundaries without a code change.

---

## 5. Glyph-height estimation

| plate layout | fraction of box height | constant |
|---|---|---|
| single row | **55%** | `GLYPH_HEIGHT_FRACTION_SINGLE_ROW` |
| two row | **27.5%** | `GLYPH_HEIGHT_FRACTION_DOUBLE_ROW` |

A single-row Indian plate is ~500×120mm with ~65mm characters. A two-row plate
stacks **two** character bands into the same box, so each band is about half as
tall.

**This is not cosmetic.** Motorcycles are almost always two-row and carry the
smallest plates on the road. Treating them as single-row would place the
worst-case class one or two buckets too high and make CCTV legibility look
better than it is. A test asserts that a 100px box scores `50-75px` as single-row
and `20-30px` as two-row.

An annotated `glyph_px` — a real character-band measurement — **always overrides
the estimate**, and the report discloses how many observations were estimated
rather than measured.

---

## 6. Sampling methodology

Interval sampling, expressed in frames per second of footage rather than a
stride: `--sample-fps 2` means two frames per second regardless of whether the
source is 25 or 30 fps.

| control | purpose |
|---|---|
| `--sample-fps` | sampling rate (default 2) |
| `--max-frames` | cap per video (default 2000) |
| `--max-seconds` | bound the window |
| `--max-per-track` | vehicle-weighted cap (default 3) |

Interval rather than random sampling: reproducible, no seek-heavy random access,
and even coverage.

**`--max-frames` thins evenly rather than truncating.** Truncating would
restrict the measurement to the beginning of the footage — which is one time of
day, one light level, possibly one traffic phase. Asserted by test.

---

## 7. Vehicle-weighted sampling and the bias it corrects

A vehicle stationary at a signal for 200 frames contributes 200 observations
under naive sampling, and the resulting "distribution" describes that one
vehicle rather than the traffic.

`vehicle_weighted(observations, max_per_track=N)` caps observations per track.
Two details:

- **Selection is evenly spaced across the track**, not the first N. The first N
  frames of a track are its entry into the scene, all at a similar distance,
  which would bias the distribution toward whatever size a vehicle happens to be
  when it first appears.
- **Deterministic** — no randomness — so a report is reproducible from the same
  footage and the same cap.

**Every report shows both weightings side by side**, with the largest
bucket-percentage shift between them, and says which to trust:

```
Largest shift between weightings: 12.40 pp. A shift this size means
frame-weighted figures are materially biased by long-dwelling vehicles;
use the vehicle-weighted column.
```

Frame count and unique vehicle/track count are always reported separately.

Where no tracker is available (the CLI does not run ByteTrack), each detection
is its own track: the two weightings coincide, and the report states the vehicle
count it actually has rather than implying one it does not.

---

## 8. Camera-level and condition reporting

Per camera: resolution, plate count, unique vehicle count, full bucket
breakdown, and p50 glyph height. Plus `p10 / p25 / p50 / p75 / p90 / p95`, min,
max and mean for estimated glyph height overall.

Breakdowns produced where the metadata supports them: **camera · day/night ·
vehicle type · front/rear · single vs two-row**.

**Categories are never invented.** Where the footage carries no such metadata
the report prints:

> Not reported — the footage carried no such metadata.

rather than a table of `unknown`, and `UNKNOWN` is the explicit value for a
missing field.

---

## 9. Privacy requirements

- **No network access. No cloud vision API. No external OCR service. No upload.**
  Everything runs locally.
- **No OCR at all** — plate text is never read, stored, printed or logged.
- Logs report counts only (`"12 frames sampled, 30 plate regions"`), never
  filenames containing plates, never plate text.
- `training/.gitignore` excludes video, image and manifest extensions. No
  footage or plate imagery is committed.
- `camera_id` is an opaque code; the observation type has no URI or credential
  field.
- **Authorization is a precondition this tool cannot enforce.** It reads any
  path it is given and cannot distinguish authorized footage from unauthorized.
  The module docstring says so explicitly. The control is the written data-use
  agreement in `docs/ANPR_M0_DATA_ACQUISITION.md` §2C, not this program.

---

## 10. Interpretation rules

1. A detector-sourced distribution is an **estimate**, never ground truth.
2. Prefer the **vehicle-weighted** column; check the shift against
   frame-weighted before trusting either.
3. An **estimated** glyph height is not a measured one; the report discloses the
   ratio.
4. Motorcycles are reported separately, because two-row plates are the
   worst-case size class.
5. Sub-20px plates are reported, not discarded.
6. A group with a small count carries a small-sample caveat — the same rule the
   rest of this project applies, and the reason the n=25 benchmark cannot
   settle anything.
7. **No CCTV number is quoted anywhere until it has been measured.**

---

## 11. Model-selection implications

The decision rule was fixed **before** any measurement, so the conclusion cannot
be chosen after seeing the numbers. `model_implication()` reports the percentage
of plates in `<20px`, `20-30px`, `<30px total`, `30-50px` and `>50px`, then
emits one of:

| scenario | trigger | recommendation |
|---|---|---|
| **A** | ≥50% of plates >50px | Proceed as planned: **CRNN + CTC** primary, **PARSeq** ceiling comparator. 32px input is comfortable. |
| **B** | 20–50px dominates | Investigate before committing: recogniser input height (32px may be too small a target); frame selection — pick the *largest* observation per track, not the first; camera configuration. **Super-resolution only if empirically justified on a real test set.** |
| **C** | >50% of plates <20px | **Capture-side work first.** No architecture recovers information that was never captured. Recommend camera placement, focal length, resolution and shutter-speed changes *before* training, and implement `INSUFFICIENT_RESOLUTION` so the system declines rather than guessing. |

Scenario C is the one worth pre-committing to, because it is the outcome most
likely to be argued away: if the pixels are not there, a model that produces
confident plate strings from them is producing fiction, and this project's
honesty rule forbids shipping that.

---

## 12. M1.5 exit criteria

| # | criterion | status |
|---|---|---|
| 1 | Analysis tool exists | **PASS** — `sizing.py` + `analyze_cctv_size.py` |
| 2 | No production code changed | **PASS** |
| 3 | Synthetic tests pass | **PASS** — 55 new tests |
| 4 | Size buckets tested | **PASS** — all six boundaries |
| 5 | Two-row handling tested | **PASS** — incl. the cross-bucket consequence |
| 6 | Vehicle-weighted sampling tested | **PASS** — incl. a case where weighting changes the answer |
| 7 | Percentile calculations tested | **PASS** — against known values |
| 8 | Documentation exists | **PASS** — this file |
| 9 | No real/private footage accessed | **PASS** |
| 10 | Existing baseline still passing | **PASS** — 851 backend + 218 training = **1069** |

---

## 13. How to run it when authorization arrives

```bash
cd training

# Preferred: measure human-drawn boxes (ground truth).
python analyze_cctv_size.py --annotations manual_sample.jsonl --out cctv_size.md

# Or: detector-estimated, from authorized footage.
python analyze_cctv_size.py /secure/footage/C-014/ \
    --camera C-014 --time-of-day night \
    --sample-fps 2 --max-frames 2000 --max-per-track 3 \
    --out cctv_size_C-014.md
```

Recommended order:

1. Run detector-estimated across all cameras and conditions for a first shape.
2. **Hand-box 100–200 plates** from the same footage; re-run with
   `--annotations` and compare. This is the calibration that says whether step 1
   can be trusted.
3. Read the scenario recommendation; record it in this document with the
   measured numbers.
4. Only then revisit the M2 architecture decision.
