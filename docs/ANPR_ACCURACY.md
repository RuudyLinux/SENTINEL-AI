# ANPR accuracy — first real measurement

**Status: MEASURED on a small public dataset (n=25). NOT validated on this
deployment's cameras.**

Until 2026-09-11 this project's ANPR accuracy was completely unmeasured, and
said so. This document records the first real numbers, how they were obtained,
and — importantly — the two documented claims they **contradicted**.

## Corpus

| | |
|---|---|
| Source | [DataCluster Labs, Indian Number Plates Dataset](https://huggingface.co/datasets/Dataclusterlabspvtltd/indian-number-plates-dataset) (HuggingFace, public sample) |
| Licence | **CC BY-NC-ND 4.0** — attribution, non-commercial, no derivatives. Commercial model training requires DataCluster Labs' permission. |
| Images in public sample | 47 (the dataset card advertises ~200; the published sample is 47) |
| Plate objects annotated | 52 |
| Plate objects **with text ground truth** | **25** (27 have bounding boxes only) |
| Distinct plates | 24 |
| States represented | KL ×10, MP ×3, UP ×2, RJ ×2, TN ×2, KA ×2, GJ ×1, WB ×1, DL ×1, MH ×1 |
| Capture style | Mobile-phone photographs, urban/rural India |

Images were **not** committed to this repository (`.gitignore` already excludes
`tools/anpr_corpus/*`), consistent with the corpus README's stance and with the
ND term of the licence.

Two corpora were derived with `tools/anpr_corpus_from_voc.py`:

- **context** — plate box expanded 2.5×, approximating the vehicle crop that
  `plate_detect.locate_plate` actually receives in `worker._run_anpr`. Tests
  localization **and** OCR.
- **tight** — the plate box alone. Skips localization, isolating OCR.

## Results after the fixes this measurement produced

The measurement found three real defects (below). After fixing them, on the
**pipeline-faithful** corpus:

| Configuration | exact BEFORE | exact AFTER | CER BEFORE | CER AFTER |
|---|---|---|---|---|
| whole-crop | 0.08 | **0.24** | 0.558 | **0.390** |
| localized | 0.00 | **0.08** | 0.753 | **0.693** |
| **localized + fallback** (current default) | — | **0.24** | — | **0.390** |

Same fixes on the OCR-isolating **tight** corpus: whole-crop 0.16 → **0.28**
exact, CER 0.524 → **0.359**; localized 0.04 → **0.12**.

So the shipping pipeline went from **0.00 exact / 0.753 CER** to **0.24 exact /
0.390 CER** — a 3× improvement in exact match and a 48% reduction in character
error rate, entirely from fixing measured defects rather than tuning.

**An honest counter-signal in the same data:** `plausible_format_rate` rose
from 0.20 to 0.64 while exact match reached 0.24, so roughly 40% of reads are
now plate-SHAPED but wrong, against ~20% before. Part of that is simply that
far more crops now yield any read at all (previously many returned `<empty>`),
but a well-formed wrong read is the dangerous class — it can clear the quality
gate. This is precisely what `plate_min_confidence`, `plate_tracker`'s
multi-frame voting and the human-review queue exist to absorb, and it is the
strongest argument for validating on **video** rather than stills, where
temporal fusion actually gets to vote.

## Original results (before the fixes)

`tools/anpr_bench.py`, EasyOCR (CPU). PaddleOCR not installed, so that row was
honestly omitted rather than estimated.

| Corpus | Configuration | Exact match | Plausible format | CER | Mean confidence | s/image |
|---|---|---|---|---|---|---|
| context 2.5× | whole-crop + EasyOCR | **0.08** | 0.40 | 0.558 | 0.447 | 2.011 |
| context 2.5× | localized + EasyOCR | **0.00** | 0.20 | 0.753 | 0.263 | 0.623 |
| tight | whole-crop + EasyOCR | **0.16** | 0.40 | 0.524 | 0.417 | 0.598 |
| tight | localized + EasyOCR | **0.04** | 0.24 | 0.636 | 0.337 | 0.209 |

Label quality was checked before drawing conclusions: **22 of 24** distinct
labels are valid Indian registrations under `anpr.PLATE_RE`. The two that are
not (`KL34F`, `KL498262`) cap achievable exact-match at 0.92 — so the measured
0.04–0.16 is **not** an artefact of bad labels. The system genuinely reads
these plates poorly.

## Three defects this measurement found (all now fixed)

**1. Two-row plates were scrambled.** OCR fragments were ordered by left-x
alone — correct only for single-row plates. India uses two-row plates widely,
and on those a pure x-sort interleaves the rows: `KL07BX7197` was read as
`INDBX7197KL07`. Fixed by clustering fragments into row bands by centre-y
(threshold = half the median glyph height, so it scales with the crop) and
ordering left-to-right within each row. This single fix drove most of the gain
above. `anpr.order_fragments`, tests in `tests/test_anpr_accuracy_fixes.py`.

**2. Localization was trusted blindly.** See below — fixed by
`anpr.better_read`: when the localized read fails the quality gate, re-read the
whole crop and keep whichever read is genuinely better. The second OCR pass is
paid only on failure, so a successful localization keeps its ~3x speed win.
Ranking is gate-pass first, then confidence, then non-empty — so a localization
miss can never turn a real read into nothing.

**3. Non-plate text was glued to real registrations.** `INDKL07BX7197` (country
marker), `SUCUNDL3CD1210` (sticker text), `KA01AJ75338E` (trailing noise) all
contained the correct plate verbatim. `anpr.extract_plate` selects the longest
contiguous substring that is a valid registration. It never invents, reorders
or substitutes characters, never touches a read that already parses, and
returns noise unchanged — so it can only turn an unusable read into a
well-formed one, never manufacture a plate. (This is the change most
responsible for the rise in plate-shaped-but-wrong reads noted above; it is
kept because the alternative is discarding correct plates outright, and the
confidence gate plus temporal fusion exist to arbitrate.)

## Two documented claims this contradicted

**1. "Plate localization is the single largest accuracy lever."** (README, now
corrected.) It was a reasoned hypothesis, never measured. On this data
localization **hurts accuracy in both corpora** — exact match 0.08→0.00 and
0.16→0.04, CER 0.558→0.753 and 0.524→0.636 — while delivering the claimed ~3×
speed win (2.011→0.623 s and 0.598→0.209 s). The speed claim survived; the
accuracy claim did not.

Likely mechanism, from the miss patterns: `_locate_classical` uses edge density
+ morphological closing bounded by `_MIN_AREA_FRACTION`/`_MAX_AREA_FRACTION`,
which assume the plate is a *small* part of a vehicle crop. Given an
already-plate-centric crop it frequently returns a sub-region — a few glyphs —
so OCR then reads nothing (`GJ01DY6855 -> <empty>`, `KA01AJ7533 -> <empty>`,
`KL03S6894 -> <empty>`). This is consistent with, but not proof of, that cause.

**2. The bundled demo asset is not usable footage.** `app/demo_assets/car-detection.mp4`
measures **320×240, 10 fps, 40 frames (4 s), 15 KB, and contains no vehicles** —
raw YOLOv8n at `conf>=0.05` detects only a "tv" (0.494). Running the real
pipeline over it (`tools/live_detect_probe.py`) produced **0 detections, 0
tracks, 0 ANPR attempts** in 13 inference passes at 137 ms each. The camera
capacity benchmark drives this same clip, so its numbers reflect a near-floor
workload, not a realistic one. README corrected accordingly.

## A real defect this surfaced

`KL07BX7197` was read as `INDBX7197KL07`. `anpr.read_plate` orders OCR
fragments by left-x only:

```python
results.sort(key=lambda r: r[0][0][0])  # sort by left x of bbox
```

On a **two-row** plate (common in India) that interleaves the rows instead of
reading top row then bottom row, and it also swallows the "IND" country marker.
Fixing it means grouping fragments into row bands by y, then ordering by x
within each band. Not yet fixed — recorded here with its reproduction.

## Engine-level levers that were tried and REJECTED (2026-09-12)

Re-measured the baseline first (same corpora, same day): context corpus
`exact 0.24 / CER 0.3896 / plausible 0.64`, tight corpus `exact 0.28 / CER
0.3593`. Three standard OCR levers were then measured against it, running the
SHIPPING configuration and changing only how EasyOCR is invoked. **None is
adopted**, and the numbers are recorded so nobody spends the afternoon
re-discovering them.

| Lever | context corpus | tight corpus | Verdict |
|---|---|---|---|
| Character allowlist (`A-Z0-9`) | exact 0.24 (=), CER 0.3723 (**better**), plausible 0.68 (better) | exact 0.28 (=), CER 0.3766 (**worse**) | **Rejected** — helps one corpus, hurts the other, moves exact match on neither. Adopting it would mean picking the corpus that flatters it. |
| `decoder="beamsearch"` | exact 0.24 (=), CER 0.3939 (worse) | exact 0.28 (=), CER 0.3593 (=) | **Rejected** — no gain, and it emits `RuntimeWarning: overflow encountered in scalar add` from easyocr's beam search. |
| Multi-scale reading (1x/2x/3x, best kept by the pipeline's own `better_read`) | exact 0.24 (=), CER 0.3853 (0.004 better) | not run after the context result | **Rejected** — 0.004 CER for +10% to +45% time is noise at n=25. |
| **A different recognition model: TrOCR** (`microsoft/trocr-base-printed`, same localization and same post-processing, only the recogniser swapped) | whole-crop exact 0.00 / CER 0.7749; localized 0.04 / 0.7922; localized+fallback 0.04 / **0.7403** | not run — the context result is not close | **Rejected, decisively** — six times worse on exact match than EasyOCR's 0.24. |

At n=25 one sample is 4 percentage points of exact match, so a CER move of
±0.017 is under half a character per plate. Every one of these is inside that
band.

TrOCR is the most informative failure of the five. It is a printed-DOCUMENT
model, and it reads a number plate as though it were prose:

    DL3CD1210  -> DISCARD1220      KA01AJ7533 -> TAX
    KL03S6894  -> LAY              KA09C2763  -> 0098263

A language-model decoder trained on receipts and scanned documents actively
hurts here, because a registration is precisely NOT a word — the prior that
makes TrOCR strong on documents is the thing that destroys it on plates. That
rules out "swap in a general-purpose OCR that scores well on text benchmarks"
as a strategy, not just this one model.

**What this means.** Exact match does not move because the remaining errors are
not parsing or invocation problems — they are the recognition model reading the
wrong glyph:

    GJ01DY6855 -> 16J0406855      KA09C2763 -> RA09C2762
    KL34A465   -> KL34A651        KL07BX7197 -> KL07BXZ197

The last one is the instructive case: `KL07BXZ197` is itself a *valid* Indian
registration, so no grammar check can reject it — the plate grammar cannot tell
that `Z` should have been `7` when both parse.

**Conclusion after five measured attempts.** Getting past 0.24 needs a
recognition model TRAINED ON NUMBER PLATES — not more post-processing, not a
different invocation of EasyOCR, and not a stronger general-purpose OCR (TrOCR
is stronger on documents and much worse here). That is a data-and-training
task: a few thousand labelled plate crops and a fine-tuning run, neither of
which exists in this repository. It is not claimed, and the 0.24 figure stands
as measured.

## Architecture pass (2026-09-12): detection, preprocessing, consensus

A second pass rebuilt the stages around the recogniser rather than replacing it,
after the five engine-level levers above all failed. Same corpus, same machine,
same day; the baseline was re-measured first and reproduced `exact 0.24 / CER
0.3896 / plausible 0.64` exactly.

**The headline result: exact match did not move.** It is 0.24 before and 0.24
after on the shipping configuration. That is stated first because it is the
number that matters and because everything else here is secondary to it. The
bottleneck identified above — the recogniser reads the wrong glyph, and no
amount of surrounding architecture fixes that — is unchanged and remains the
thing standing between this system and a better figure.

What did move is the **false-positive rate**, which was the second priority and
is arguably the more dangerous failure for a police system: a plate-shaped but
wrong read clears the quality gate, becomes a real `Vehicle` row, and can raise
a watchlist alert about a vehicle that was never there.

### Measured, whole-crop configuration, n=25

| | before | after | |
|---|---|---|---|
| exact match | 0.24 | **0.24** | unchanged |
| character error rate | 0.3896 | 0.3983 | 0.009 worse — under half a character per plate, inside the noise band |
| plate-shaped reads | 0.64 | **0.52** | 12 points fewer plate-shaped-but-wrong reads |
| gate acceptance | 0.48 | 0.44 | the gate accepts less |
| **false-positive rate** | 0.28 | **0.24** | wrong reads that were nonetheless accepted |
| **false positives among accepted** | 0.5833 | **0.5455** | if it claims a plate, how often it is wrong |

The cause is the **state/UT code check** in `looks_like_plate`. The format regex
alone accepts any two letters, so `QQ00QQ0000` and `XX12AB1234` were valid
plates. Requiring a prefix an Indian state or union territory actually issues
tests the read against reality rather than against a pattern, and it costs
nothing at runtime. The small CER regression is the price: a read whose
plate-shaped substring is now rejected falls back to the longer raw string.
That trade was taken deliberately — a wrong plate that looks real is worse than
a read that is visibly garbage.

`FPofAcc` is still 0.55. **Over half of everything the gate accepts is wrong.**
That is not fixed and is not claimed to be; it is the direct consequence of the
recogniser's accuracy, and it is why temporal consensus and the human-review
queue exist.

### The opt-in multi-variant mode

Reading the crop through several preprocessing renderings and selecting by
cross-variant AGREEMENT (not by confidence):

| configuration | exact | CER | FP | FPofAcc | s/image |
|---|---|---|---|---|---|
| whole-crop (baseline) | 0.24 | 0.3983 | 0.24 | 0.5455 | 1.91 |
| localized+fallback (**shipping default**) | 0.24 | 0.3939 | 0.24 | 0.5455 | 2.12 |
| multi-variant, agreement (**opt-in**) | 0.28 | 0.3506 | 0.20 | 0.4545 | 5.79 |

Exact match 0.24 → 0.28 is **one sample out of 25**. At this corpus size one
sample is four percentage points, and this document has said from the start to
treat one- or two-sample differences as noise. It is **not** an accuracy
improvement and is not shipped as the default. CER 0.3896 → 0.3506 is a firmer
signal (it aggregates over ~250 characters, not 25 binary outcomes), as is the
FP drop, but both cost 2.7x the OCR time — the most expensive operation in the
camera loop. `PLATE_PREPROCESS_VARIANTS` makes it an explicit operator choice.

### Selection strategy: agreement, not confidence

Measured over all seven variants on the same 25 images:

| selection | exact | CER | FP | FPofAcc |
|---|---|---|---|---|
| highest confidence | 0.24 | 0.3160 | 0.40 | 0.62 |
| **most-agreed text** | 0.28 | 0.2814 | 0.32 | 0.53 |

Picking the most confident of N reads **raised false positives from 0.28 to
0.40**. It is also a biased estimator: the maximum of N samples is
systematically larger than any one of them, so adopting it would have inflated
the recorded confidence of every read (mean 0.446 → 0.574) and silently loosened
both `plate_min_confidence` and `plate_review_confidence_floor` — auto-accepting
reads a single-variant pipeline would have sent to a human. Rejected.

**Agreement predicts correctness far better than confidence does.** Over the
same reads:

| variants agreeing | n | correct | precision |
|---|---|---|---|
| ≤ 2 of 7 | 13 | 0 | **0.00** |
| 3 of 7 | 5 | 1 | 0.20 |
| 4 of 7 | 3 | 2 | 0.67 |
| ≥ 5 of 7 | 4 | 4 | **1.00** |

| OCR confidence | n | correct | precision |
|---|---|---|---|
| ~0.2 | 6 | 0 | 0.00 |
| ~0.4 | 8 | 2 | 0.25 |
| ~0.6 | 5 | 1 | **0.20** |
| ~0.8 | 6 | 4 | 0.67 |

Agreement is monotone; confidence is not (it falls between 0.4 and 0.6). This is
why `variants_agreeing` is stored and gated on as its own signal and is never
folded into the confidence number.

### Escalation was measured and rejected

Running variants only when the cheap read FAILS the gate seemed the obvious way
to get the benefit at low cost. It does not work: it fires only when the first
read fails, but the crops variants actually rescue are ones where the first read
**passes with a wrong answer**.

| | exact | CER | FP | OCR calls/plate |
|---|---|---|---|---|
| escalate on failure (3 variants) | 0.24 | 0.3160 | 0.32 | 2.04 |
| always (3 variants) | 0.28 | 0.3030 | 0.28 | 3.00 |

Exact match stays at baseline and false positives rise. Recorded so the idea is
not re-attempted.

### What was NOT measured

Temporal consensus — the change with the strongest expected effect on real
deployments — **cannot be measured on this corpus at all.** These are 25
independent still photographs; there are no tracks and no second frame of the
same vehicle, so multi-frame voting never gets to vote. Its benefit on video is
a reasoned expectation, not a measurement, and is flagged as such here rather
than folded into the numbers above. Measuring it needs the real footage listed
under "What is still needed for a real figure".

The same applies to plate-detection quality: `detection_success_rate` is 0.64 on
this corpus with `mean_detection_confidence` 0.6649, but that confidence is the
classical localizer's **geometric plausibility score**, not a trained detector's
probability. The two are never pooled, and the score must not be read as a
detection probability.

## A1 — Precision hardening: confidence cannot gate false positives (2026-09-12)

The master plan's A1 item proposed sweeping the ANPR quality gate to buy
precision without new data. **The sweep refuted its own premise**, and the
result is recorded here because the negative finding is more useful than the
change it was meant to justify.

### The sweep

Shipping read path (localized + whole-crop fallback), same 25-plate corpus,
confidence threshold varied. Diagnostic use of the benchmark corpus, which its
CC BY-NC-ND licence permits — no training, no derivative model.

| threshold | accepted | correct | wrong | precision | recall | FP rate | FP-of-accepted |
|---|---|---|---|---|---|---|---|
| 0.05-0.25 | 13 | 6 | 7 | 0.462 | 0.240 | 0.280 | 0.538 |
| **0.35 (default)** | 11 | 5 | 6 | **0.455** | **0.200** | 0.240 | **0.545** |
| 0.45-0.50 | 8 | 4 | 4 | 0.500 | 0.160 | 0.160 | 0.500 |
| 0.65-0.70 | 6 | 3 | 3 | 0.500 | 0.120 | 0.120 | 0.500 |
| 0.85-0.90 | 2 | 1 | 1 | 0.500 | 0.040 | 0.040 | 0.500 |

**No threshold reaches precision above 0.5.** Not one. Raising the floor from
0.35 to 0.45 moves precision 0.455 to 0.500 — one sample at n=25, inside the
noise band — while cutting recall from 0.200 to 0.160. That is not a trade worth
making, and it does not fix anything.

### Why: the confidence signal does not separate the populations

| population | n | min | median | max |
|---|---|---|---|---|
| correct reads | 6 | 0.262 | 0.722 | 0.990 |
| wrong, plate-shaped reads | 7 | 0.260 | 0.638 | 0.956 |

**Six of the seven wrong reads sit at or above the LOWEST correct read's
confidence.** The distributions almost entirely overlap. A threshold can only
separate two populations to the extent they differ, so no choice of
`plate_min_confidence` fixes precision — it only trades recall away.

**Conclusion: `plate_min_confidence` was NOT changed.** There is no defensible
value to move it to.

### The defect this surfaced, and the fix

The worst single case: **`UP84AE9889` was read as `UP81AE9889` at confidence
0.956.** A wrong plate, one character off, at near-maximal confidence.

`rules_engine.evaluate` gated watchlist severity on `plate_confidence` alone. So
that single frame would clear the 0.60 floor, raise a **CRITICAL** alert, and
**auto-open an incident** — naming a vehicle that was never there. In a police
deployment that is a wrongful-stop risk, and it is precisely what the temporal
layer was built to prevent and was not being asked to.

Fixed: **CRITICAL now requires a confident read AND corroboration across
frames.** The two are independent signals and both must pass.

- The match is never silenced — an uncorroborated hit still fires at **HIGH**,
  with the reason string saying `UNCORROBORATED … only ONE frame supports the
  read — requires confirmation`.
- `Vehicle.plate_corroborated` carries the signal, maintained by
  `correlate.upsert_vehicle_for_plate` and set from
  `plate_tracker.has_consensus`. It ratchets up only: a vehicle identified well
  at one camera is not downgraded by a glimpse at the next.
- NULL (rows predating the column, and the legacy single-frame path) reads as
  NOT corroborated. The safe default for a missing safety signal is "not
  satisfied".
- `WATCHLIST_REQUIRE_CORROBORATION=false` restores the previous behaviour in one
  env var.

Why corroboration rather than a better threshold: it is the only signal measured
to separate correct from wrong reads. Phase 2 found the same pattern across
preprocessing variants — agreement was monotone with correctness (≤2 of 7
agreeing: 0 of 13 correct; ≥5 of 7: 4 of 4) while confidence was not. Agreement
across observations carries information that confidence does not.

**This does not improve accuracy, and no accuracy claim is made.** Exact match
remains **0.24** and CER **0.3983**. What changes is the consequence of a wrong
read: it can no longer auto-escalate to CRITICAL or open an incident on the
strength of one frame.

**Not yet measured:** the real-world precision effect. This corpus has no
tracks, so corroboration cannot fire on it at all — the gate's benefit is a
reasoned expectation backed by the variant-agreement evidence, not a
measurement, and it stays that way until a video benchmark exists.

Tests: `tests/test_watchlist_confidence_gating.py::TestCorroborationGate` (6).

## Phase 2 — Failure Analysis

Where the remaining errors actually come from. No production code was changed
by this phase; it is measurement and inspection only. The baseline numbers in
the sections above are unchanged.

### 1. Methodology

Three things were done that the earlier passes did not do.

**Ground-truth plate boxes were recovered.** Each corpus sample's filename maps
back to its source image and Pascal VOC annotation, so the human-drawn plate box
is known in the corpus image's own coordinate space. That makes localization
scorable by IoU against a human, instead of being inferred from whether the OCR
string looked right.

**OCR was run at three localization levels on every sample:**

| level | what OCR was given |
|---|---|
| `whole` | the whole 2.5x context crop — what the shipping pipeline reads when localization misses, which is most of the time |
| `detect` | the region this repo's detector found |
| `gtbox` | **the human-drawn plate box** — the perfect-localization oracle |

The `gtbox` column is the load-bearing one. If OCR is still wrong when handed a
box a human drew, localization is not what is costing the read, and no amount of
detector work can recover it.

**Every plate crop was looked at.** The 25 ground-truth plate regions were
exported, upscaled and inspected visually, with the OCR output and quality
metrics beside each one, before any sample was categorized. Objective measures
(variance of Laplacian for blur, RMS contrast, brightness, clipping, glyph
height) were computed as indicators, but each was confirmed by eye — Laplacian
variance in particular scales with both resolution and contrast, so a small
sharp plate and a large soft one can score alike.

### 2. Dataset

The same n=25 labelled corpus as every other measurement in this document
(DataCluster Labs, CC BY-NC-ND, 25 plates with text ground truth). Same
machine, same day, same pipeline configuration.

Note the corpus is built by expanding the annotated plate box 2.5x, so it
approximates a vehicle crop. **Vehicle detection is therefore bypassed entirely
and cannot be scored here** — every sample arrives already centred on a vehicle.
Vehicle-detection failures are not zero in the real world; they are simply not
measurable on this corpus, and are reported as unknown rather than as zero.

Plate crops are mostly generous: median glyph height 75px, median RMS contrast
47. This is mobile-phone photography, not CCTV. Real CCTV plates will be
smaller, and the resolution-related conclusions below will not transfer.

### 3. Per-sample failure table

`whole` = shipping path read, `gtbox` = read with a human-drawn plate box.
Detected = the detector's best box reached IoU >= 0.3 against the true plate.

| ID | Ground truth | OCR (whole) | OCR (gtbox) | Plate detected | Crop readable by a human | Primary failure | Secondary |
|---|---|---|---|---|---|---|---|
| 1 | DL3CD1210 | **DL3CD1210** | SUCUKIDLBCD1210 | no (IoU 0.00) | yes, crisp | — (shipping correct) | GT box includes the SUZUKI badge, which only hurts `gtbox` |
| 2 | GJ01DY6855 | 16J040V6E55 | GJ04OL6855 | no | yes, small + stylized | character recognition | insufficient resolution (18px glyphs) |
| 3 | KA01AJ7533 | KA04L7533 | GA01AL7533 | no | yes, washed out | character recognition | low contrast (20.7) |
| 4 | KA09C2763 | KA09CZ763 | **KA09C2763** | no | yes, crisp | crop too loose | — |
| 5 | KL03S6894 | `<empty>` | 036854 | no | **marginal** | low contrast (9.1) | defocus blur |
| 6 | KL07BX7197 | **KL07BX7197** | **KL07BX7197** | no | yes, crisp | — success | — |
| 7 | KL10AG7249 | **KL10AG7249** | **KL10AG7249** | no | yes, crisp | — success | — |
| 8 | KL34A465 | **KL34A465** | **KL34A465** | no (IoU 0.03) | marginal, oblique | — success | — |
| 9 | KL34A465 | `<empty>` | `<empty>` | no | **marginal** | perspective/angle | insufficient resolution |
| 10 | KL34F | `<empty>` | 5 | **yes (IoU 0.94)** | **marginal** | defocus blur | ground-truth label is not a valid registration |
| 11 | KL35F4337 | KL35FL327 | **KL35F4337** | **yes (0.92)** | yes, crisp | crop too loose | — |
| 12 | KL35H5834 | **KL35H5834** | W35H5834 | no | yes, glare stripe | — success | glare costs the `gtbox` read (K+L merged to W) |
| 13 | KL41L7001 | KL41L7OO1C20 | **KL41L7001** | **yes (0.77)** | yes, crisp | crop too loose ("i20" badge) | — |
| 14 | KL498262 | KLA98E62WLUIV | KLA98262 | no | yes, **handwritten italic** | non-standard font | ground-truth label is not a valid registration |
| 15 | MH18AA1002 | MH4BAACO2 | MHHBAA60O2 | **yes (0.72)** | yes, soft | defocus blur | character recognition |
| 16 | MP04PA0434 | **MP04PA0434** | MRO4PA0434 | no (0.02) | yes, crisp | — success | — |
| 17 | MP07L7524 | MP0TSL7524 | MP07SL7524 | no | yes, crisp | extra character — the mounting **bolt** between groups is read as `S` | — |
| 18 | MP13GA9462 | HP13GA9462 | MP13G4946 | no | yes, angled | character recognition | perspective |
| 19 | RJ11GB1829 | RJGB111829 | RJGB31829 | no | yes, **italic + slash separator** | non-standard font | character segmentation |
| 20 | RJ11GB8850 | RO11GE8850 | RO11GB8850 | no | yes, **italic**, stripe overlay | non-standard font | character recognition |
| 21 | TN58AP5280 | **TN58AP5280** | **TN58AP5280** | no | yes, crisp | — success | — |
| 22 | TN58D5353 | TN5DS353 | TN5DS353 | no | yes, **handwritten** on rusted plate | non-standard font | low contrast |
| 23 | UP84AE6664 | 0CUPBLAE66647IND… | UP8LAE6664 | no | yes, large + crisp | character recognition (`4`→`L`) | crop too loose (whole) |
| 24 | UP84AE9889 | INDUP8LAE9E89 | INDUP8ELAE9889 | no | yes, crisp | character recognition (`4`→`EL`) | "IND" marker inside the GT box |
| 25 | WB42AX7446 | EXLZAHBINDL2AX7LL6 | HBZZAX7L46 | no | **yes, crisp + high contrast** | character recognition | — |

Sample 25 is the single most informative row in this table. `WB42AX7446` is a
large (113px glyphs), sharp, high-contrast, front-on, standard-font plate that
any human reads instantly, and OCR returns `HBZZAX7L46` — four wrong characters
with a perfect image and a perfect box.

### 4. Failure distribution

Rates:

| measure | value |
|---|---|
| vehicle detection success | **not measurable** — the corpus is pre-cropped to vehicles |
| plate detector found *some* region | 16/25 = 0.64 |
| plate detector found *the plate* (IoU >= 0.3) | **4/25 = 0.16** |
| mean IoU against the human-drawn box | **0.136** |
| plate crop readable by a human | 22/25 = **0.88** (3 marginal) |
| exact match, shipping path | 7/25 = 0.28 |
| exact match, perfect localization | **7/25 = 0.28** |
| character error rate (shipping, documented baseline) | 0.3983 |

Primary bottleneck of the 18 failures:

| primary cause | count | share of failures |
|---|---|---|
| **character recognition** | **10** | **56%** |
| localization / crop too loose | 3 | 17% |
| image quality (blur, contrast, angle) | 3 | 17% |
| invalid ground-truth label (unmatchable) | 2 | 11% |
| validation/logic | **0** | 0% |

No correct read was ever rejected by the validation gate on the shipping path.
One correct read (`KA09C2763`) fell below the confidence floor at 0.299 on the
`gtbox` path — the gate working as designed on a genuinely uncertain read, not a
logic error. Two labels (`KL34F`, `KL498262`) are not valid registrations and
cap achievable exact match at 23/25 = 0.92, as already documented.

### 5. Character confusion analysis

Across the perfect-localization reads: **57 edit operations — 25 substitutions,
20 missing characters, 12 invented characters.**

**Only 5 of the 25 substitutions are the six "classic" confusions**
(`0↔O 1↔I 2↔Z 5↔S 6↔G 8↔B`) that the grammar-based repair is built to handle.
The other 20 are not:

```
4 -> L   x3        D -> O          L -> W
0 -> O   x2  [classic]   Y -> L     4 -> A
3 -> B             K -> G          1 -> H
1 -> 4             J -> L          8 -> B  [classic]
                   9 -> 5
                   F -> 5
```

Missing characters: `K` x4, `L` x3, `4` x3, `3` x2, `1` x2.
Invented characters: `S` x2, `U` x2, `I` x2.

Two conclusions:

1. **The confusion set the decoder targets explains 20% of the substitutions.**
   Extending the confusion table is not the lever — `4→L`, `Y→L`, `K→G`, `J→L`,
   `L→W` are shape confusions no positional grammar rule can safely undo,
   because both characters are legal in those positions.
2. **32 of 57 edits are insertions and deletions, not substitutions.** That is a
   *segmentation* failure — the recogniser is not splitting the glyph run
   correctly — and it is structurally invisible to any character-substitution
   repair, which by construction preserves length.

### 6. Plate localization analysis

The detector is genuinely weak: mean IoU 0.136, and 12 of its 16 detections
landed on something that is not the plate. Only 4 of 25 reached IoU >= 0.3.

**And fixing it would not help.** Handing OCR a human-drawn box gives exact match
7/25 — *identical* to reading the whole crop:

| | count |
|---|---|
| correct with BOTH whole crop and perfect box | 4 |
| correct ONLY with a perfect box | **3** (`KA09C2763`, `KL35F4337`, `KL41L7001`) |
| correct ONLY with the whole crop | **3** (`DL3CD1210`, `KL35H5834`, `MP04PA0434`) |
| wrong either way | **15** |

Perfect localization recovers 3 samples and **breaks 3 others** — a net change of
zero. The three it breaks are cases where the surrounding context helps EasyOCR
segment, and a tight box removes it.

This is the clearest single result of Phase 2: **an upper bound of +0.12 exact
match from any conceivable localization improvement, with a measured net effect
of 0.00**, against 15 of 25 samples that are wrong no matter how the plate is
cropped.

The detector is still worth fixing for cost and evidence quality — a correct
plate box is what `plate_bbox` records and what an operator reviews — but it is
not an accuracy lever, and it should not be funded as one.

### 7. Preprocessing analysis

From the 7-variant dump (offline; multi-variant remains disabled in production):

| | value |
|---|---|
| `original` variant correct | 6/25 = 0.24 |
| **some** variant correct (oracle ceiling) | 9/25 = 0.36 |
| rescued by a non-`original` variant | **3** (`KL07BX7197`, `KL34A465`, `KL41L7001`) |
| **no variant ever correct** | **16/25 = 0.64** |

Per-variant exact match: `original` 0.24, `gray` 0.24, `clahe` 0.24, and
`denoise`/`sharpen`/`adaptive`/`otsu` all 0.20. **No variant beats the original.**

Churn is high: in **15 of the 16 unfixable samples**, the variants produced more
than one distinct *wrong* answer — seven distinct wrong answers in seven cases.
Preprocessing is mostly converting one wrong read into a different wrong read,
which is why selection has to be by agreement and why the agreement signal works
at all.

**Preprocessing cannot fix 64% of the corpus.** It is not claimed to improve
accuracy: exact match with the best selection strategy is 0.28 against a baseline
0.24, one sample, inside this corpus's noise band.

### 8. Main bottleneck

**Option B — recognition — by a clear margin.**

The evidence, in order of strength:

1. Perfect, human-drawn localization changes exact match by **nothing**
   (0.28 → 0.28), with 15/25 wrong regardless of the crop.
2. **88% of plate crops are readable by a human**, and the failures are
   concentrated in crisp, well-exposed, correctly-cropped images —
   `WB42AX7446` at 113px glyphs and 54.7 contrast reads as `HBZZAX7L46`.
3. **80% of substitutions are outside the classic confusion set**, and 56% of
   all edits are insertions/deletions, i.e. segmentation, which no
   post-processing rule can address.
4. No variant of preprocessing beats the unprocessed image, and 64% of samples
   are unfixable by any of them.
5. Validation/logic contributes **zero** failures.

Ranked:

| rank | bottleneck | failures | can it be fixed by engineering here? |
|---|---|---|---|
| 1 | **Character recognition** | 10 of 18 (56%) | No — needs a plate-trained recogniser |
| 2 | Image quality | 3 of 18 (17%) | Partly — capture-side, not software |
| 3 | Localization / crop | 3 of 18 (17%) | Yes, but **net accuracy gain measured at 0.00** |
| 4 | Invalid ground truth | 2 of 18 (11%) | Corpus problem, caps ceiling at 0.92 |

A distinct sub-class worth separating: **5 of the 10 recognition failures involve
non-standard plates** — two handwritten (`KL498262`, `TN58D5353`), two italic
script (`RJ11GB1829`, `RJ11GB8850`), one with a mounting bolt read as a
character (`MP07L7524`). These are not "OCR is imprecise"; they are plates
outside the font distribution any general-purpose recogniser was trained on, and
they are common on Indian roads.

**Option D (temporal fusion) cannot be assessed on this corpus at all** — 25
independent stills, no tracks, no second frame of any vehicle. It is not ranked
because there is no evidence either way, and none is claimed.

### 9. Recommended next milestone

Unchanged from the milestone already recorded above, now supported by
sample-level evidence rather than by elimination:

> **A plate-trained recognition model, evaluated against a larger labelled
> CCTV/video benchmark.**

Two additions Phase 2 justifies:

- The training set must include **non-standard Indian plates** — handwritten,
  italic/script, and bolt-obstructed — because they are a fifth of the
  recognition failures here and a general-purpose recogniser has no prior for
  them.
- The benchmark must be **video**, so temporal consensus (built, untested) can
  finally be measured, and so `detect_every_n_frames` and the throttling
  behaviour are exercised.

**Estimated impact, stated as a bound rather than a forecast:** 15 of 25 samples
are wrong under perfect localization and perfect preprocessing. Those 15 are the
addressable pool for a better recogniser. Of them, 12 have human-readable crops,
so an upper bound on recogniser-driven improvement is roughly **0.28 → 0.76**
exact match *if* recognition became perfect on readable plates — which it will
not. No specific post-training figure is predicted, because nothing measured
here supports one.

### 10. Limitations

1. **n=25.** One sample is four percentage points. Every count in the failure
   distribution carries that granularity; "10 recognition failures" is
   10 ± a couple, not a precise share.
2. **Primary-cause attribution involves judgement.** Most samples have more than
   one contributing factor, and the split between "character recognition" and
   "image quality" on soft or low-contrast crops is a call made by looking at
   the crop. The per-sample table is published so those calls can be disputed
   individually.
3. **Vehicle detection is not measured** — the corpus is pre-cropped. Its real
   failure rate is unknown, not zero.
4. **Mobile-phone photography, not CCTV.** Median glyph height is 75px; real
   CCTV plates are far smaller, so the finding that resolution is rarely the
   bottleneck almost certainly does NOT transfer to the target deployment. On
   CCTV, resolution and motion blur should be expected to matter much more.
5. **Temporal consensus is unmeasured**, and this corpus structurally cannot
   measure it.
6. Laplacian variance as a blur proxy is scale- and contrast-dependent; it was
   used as an indicator alongside visual inspection, never alone.
7. The `whole` column here reads a CLAHE-preprocessed crop and scores 0.28,
   while the headline baseline of 0.24 reads the raw crop. The one-sample
   difference is inside the noise band and **no claim is made that CLAHE
   improves accuracy**; the baseline figures in this document are unchanged.

## Honest limitations

1. **n=25.** A single sample is 4 percentage points of exact-match. Treat
   differences of one or two samples as noise; the *direction* of the
   localization result is consistent across both corpora, which is why it is
   reported at all.
2. **Phone photos, not CCTV.** Different resolution, angle, motion blur and
   compression from a real camera feed. Absolute numbers will not transfer.
3. **Not this deployment's cameras.** Nothing here describes Gujarat Police
   camera performance.
4. **Crops approximate vehicle boxes**; a real YOLO vehicle crop differs.
5. **Temporal fusion is not exercised.** These are single stills;
   `plate_tracker`'s multi-frame voting — arguably the pipeline's strongest
   accuracy mechanism — cannot help and is therefore unmeasured.
6. **No PaddleOCR comparison.** Attempted on 2026-09-12 and abandoned for a
   real environment conflict, not for lack of trying. Installing
   `paddlepaddle` + `paddleocr` downgraded numpy (2.4.6 -> 2.3.5) and broke
   torch outright:

       OSError: [WinError 127] The specified procedure could not be found.
       Error loading "...	orch\lib\shm.dll" or one of its dependencies.

   Restoring numpy fixed torch, but `import paddle` then loads its own
   MKL/OpenMP DLLs which shadow torch's, so torch fails the same way in any
   process that has imported paddle first — and paddleocr pulls torch in. The
   two cannot share a process on this machine, and torch is what YOLOv8 and
   EasyOCR (the shipping pipeline) run on, so paddle was uninstalled and the
   stack verified back to health: `704 passed`, and the benchmark reproduces
   `exact 0.24 / CER 0.3896` unchanged. A PaddleOCR comparison needs a
   separate environment; `tools/anpr_bench.py` already has the configuration
   and skips it with `skipping 'localized + paddleocr' - engine not installed`.

## Next accuracy milestone (a separate, not-yet-started task)

Everything measured above points at one conclusion, reached independently twice:
**the bottleneck is character recognition, and nothing around it will move exact
match.** Five engine-level levers were tried and rejected; an architecture pass
then rebuilt detection, preprocessing, candidate selection and temporal
consensus, and exact match stayed at 0.24.

The next milestone is therefore explicitly **not** more regex rules, more
preprocessing variants, or another general-purpose OCR engine. All three have
been measured and none of them is the lever. It is:

> **A plate-trained recognition model, evaluated against a larger labelled
> CCTV/video benchmark.**

Two halves, both required, neither of which exists in this repository today:

1. **A recognition model trained on number plates.** Not a document OCR — TrOCR
   is stronger than EasyOCR on printed documents and six times worse here,
   because a registration is precisely not a word and a language-model decoder
   actively hurts. This needs a few thousand labelled plate crops and a
   fine-tuning run. It is a data-and-training task, with its own licensing
   question about the training corpus.

2. **A larger labelled benchmark, on video rather than stills.** The current
   n=25 corpus cannot measure temporal consensus AT ALL — there are no tracks
   and no second frame of any vehicle — and at that size one sample is four
   percentage points of exact match. 100-300 plates across day / night / motion
   blur / oblique angle / glare / rain / distance, as video, would make both the
   recognition figure and the fusion figure real.

Until both exist, **0.24 stands as measured** and no improvement to recognition
accuracy is claimed.

## What is still needed for a real figure

1. **Real footage from the target cameras** — video, not stills, so temporal
   fusion is measured too. 100–300 plates across day / night / motion blur /
   oblique angle / glare / rain / distance.
2. Ground-truth text per plate. `tools/anpr_corpus_from_voc.py` accepts Pascal
   VOC with a `number_plate_text` attribute (CVAT, makesense.ai, LabelImg all
   export this), so annotation tooling output can be used directly.
3. Then: `python tools/anpr_bench.py <corpus> --json results.json`.

## Reproducing this measurement

```bash
cd backend
# 1. fetch the public sample (47 images + Pascal VOC annotations)
#    https://huggingface.co/datasets/Dataclusterlabspvtltd/indian-number-plates-dataset
# 2. convert to the benchmark's corpus layout
python tools/anpr_corpus_from_voc.py <dataset_root> --out /tmp/anpr_ctx
python tools/anpr_corpus_from_voc.py <dataset_root> --out /tmp/anpr_tight --tight
# 3. measure
python tools/anpr_bench.py /tmp/anpr_ctx   --json ctx.json
python tools/anpr_bench.py /tmp/anpr_tight --json tight.json
```

Dataset attribution: DataCluster Labs, *Indian Number Plates Dataset*,
CC BY-NC-ND 4.0.
