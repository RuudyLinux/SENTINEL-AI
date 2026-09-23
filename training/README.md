# ANPR dataset tooling

Schema, splitting, quality control and evaluation metrics for the Indian
license-plate recognition dataset described in
`docs/ANPR_PHASE3_TRAINING_PLAN.md` and `docs/ANPR_M0_DATA_ACQUISITION.md`.

**No model is trained here and no real plate data lives here.** Dataset images
stay in the controlled environment described in the M0 document; this directory
holds only tooling, and the tests run entirely on synthetic fixtures.

## Running

```bash
cd training
python -m pytest tests -q
```

Requires nothing but Python 3.11 and pytest — no GPU, no OCR engine, no camera,
no network, no external dataset, no model weights.

## Layout

```
schema.py            the canonical JSONL record and its validation
split.py             deterministic vehicle-disjoint splitting + leakage detection
qc/checks.py         ERROR / WARNING / INFO dataset checks
qc/report.py         QC, character-coverage and plate-size reports
evaluate/metrics.py  exact match, CER, edit operations, Wilson CIs,
                     McNemar paired comparison, confidence calibration
fixtures.py          synthetic records for testing (no real plates, no images)
```

## The two rules this tooling enforces

**1. Annotation validity is not vehicle-registration validity.** `KL34F` is a
valid annotation of a real plate and an invalid Indian registration. The schema
accepts it; QC flags it for review; nothing drops it. Auto-rejecting unusual
labels would delete exactly the non-standard plates that Phase 2 measured as 5
of 10 recognition failures.

**2. One vehicle identity belongs to exactly one split.** Splitting on frames
puts the same plate under the same lighting in both train and test, and the
model is scored on its own training data. The split is a pure function of the
identity string, and CI fails the build if any identity crosses splits.
