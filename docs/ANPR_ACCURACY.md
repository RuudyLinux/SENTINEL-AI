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
6. **No PaddleOCR comparison** (not installed).

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
