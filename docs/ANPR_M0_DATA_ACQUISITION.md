# M0 — ANPR data acquisition & licensing

**Status: BLOCKED.** No lawfully usable training source is in hand. The
engineering specifications below are finalized and ready; the authorization and
licensing items are not, and they gate everything downstream.

This document follows the same convention as the rest of this project's
governance material: it provides **mechanism, not policy**, and it states what
it does **not** provide rather than implying coverage it lacks. Nothing here is
legal advice, and nothing here is a compliance claim. Items needing legal,
privacy or security sign-off are marked **[REVIEW REQUIRED]**.

---

## 1. Why M0 exists

Phase 2 established that character recognition is the dominant addressable
bottleneck. Phase 3 established that the binding constraint on fixing it is not
the model — every candidate architecture is permissively licensed and
straightforward to train — but **lawfully usable, representative training data**,
of which this project currently has none.

Two independent facts make training impossible today:

1. **The only corpus in hand is licensed in a way that forbids this use.**
2. **The only corpus in hand cannot validate a result even if we had one** — at
   n=25 a new model must score **0.604** before the improvement is
   statistically distinguishable from the 0.24 baseline
   (`docs/ANPR_PHASE3_TRAINING_PLAN.md` §4).

---

## 2. Data sources

### A. Existing benchmark corpus (n=25)

| | |
|---|---|
| Source | [DataCluster Labs, Indian Number Plates Dataset](https://huggingface.co/datasets/Dataclusterlabspvtltd/indian-number-plates-dataset) (public sample) |
| Licence | **CC BY-NC-ND 4.0** |
| Content | 47 images, 52 annotated plates, **25 with text ground truth**, 24 distinct plates |
| Capture | Mobile-phone photography, urban/rural India |
| Current use | **Regression and diagnostics only** |

**Permitted here:** benchmarking, regression testing, failure analysis. This is
how it is used today and that use continues.

**NOT permitted, and not done:**

- **Training or fine-tuning any model.** *NoDerivatives* bars producing a
  derived work — a trained model is the clearest possible example — and
  *NonCommercial* bars a police/government deployment.
- Redistribution of the images, the crops, or derived annotations.
- Committing any of it to this repository. `.gitignore` already excludes
  `tools/anpr_corpus/*`, and the working copies used for Phase 2 were kept in a
  scratch directory outside the repo.

**Limitations beyond licensing** — these matter even if the rights were granted
tomorrow:

| limitation | measured value |
|---|---|
| sample size | n=25 — cannot detect an improvement below +36.4 pp |
| character coverage | **`I O Q V Z` never appear**; 12 more classes appear ≤2 times; 231 total character instances |
| capture modality | phone photography, **not CCTV** — median glyph height **75.4px**, max 595px |
| temporal structure | independent stills — **no tracks**, so temporal fusion is unmeasurable |
| label validity | 2 of 24 labels are not valid registrations, capping achievable exact match at 0.92 |
| vehicle-type coverage | unrecorded; no motorcycle/commercial/night/rain stratification |

**Decision: keep as the frozen regression benchmark. Never train on it** unless
DataCluster Labs grants explicit written rights covering model training and
government deployment. The 0.24 / 0.3983 baseline is anchored to this corpus and
must not move.

### B. Licensed external dataset

DataCluster Labs advertises a full commercial dataset (15,000+ annotated images)
separate from the CC BY-NC-ND public sample. **No licence terms for that tier
are published**, so every downstream right is currently **UNKNOWN**.

A commercial dataset licence does **not** automatically permit model training,
derivative-model redistribution, or government deployment. These are distinct
grants and each must appear explicitly in the contract.

#### Licensing checklist — to be answered in writing by any vendor before purchase

| # | question | acceptable answer |
|---|---|---|
| 1 | May the data be used to **train** a machine-learning model? | explicit yes, in the licence text |
| 2 | May it be used to **fine-tune** a pretrained model? | explicit yes |
| 3 | May the resulting **trained model** be used commercially? | explicit yes |
| 4 | May it be deployed by a **government / law-enforcement** body? | explicit yes — many licences carve this out |
| 5 | Is the trained model a **derivative work** under the licence, and if so what restrictions follow it? | stated unambiguously |
| 6 | May the **trained model weights** be redistributed, or delivered to the deploying agency? | explicit yes |
| 7 | May **derived annotations or crops** be redistributed to the agency? | explicit yes or an explicit no we can design around |
| 8 | May the data be used for **internal evaluation** separately from training? | explicit yes |
| 9 | Is the vendor's **own right to license** the images warranted (subjects, photographers, road authorities)? | contractual warranty + indemnity |
| 10 | Does the licence **survive** for the model's deployed lifetime, or expire? | perpetual for already-trained models, ideally |
| 11 | Are there **attribution** obligations, and where must attribution appear? | stated |
| 12 | Are there **audit or reporting** obligations to the vendor? | stated — a police deployment may be unable to comply |
| 13 | What are the **termination** consequences for an already-trained, already-deployed model? | stated |
| 14 | **Territorial** scope? | India at minimum |

**Any question without an explicit written answer is recorded as
`UNKNOWN — legal review required`, and blocks purchase.** [REVIEW REQUIRED]

### C. Our own CCTV data — the preferred long-term source

This is the recommended source, for three reasons that are not about cost:

1. It is the only source whose distribution **matches the deployment** — real
   camera angles, real mounting heights, real plate pixel sizes, real night
   behaviour, real compression. The current corpus's 75px median glyph height
   is not what these cameras will see.
2. It produces **video**, which is the only way `plate_tracker`'s temporal
   consensus — built and unit-tested since Phase 1, never measured — can be
   evaluated at all.
3. Its rights position can be made unambiguous in a single written agreement
   with the operating authority, rather than inferred from a third party's
   licence.

**It cannot begin without written authorization.** [REVIEW REQUIRED]

---

## 3. Data governance for collected CCTV footage

Where the platform already implements a control, the dataset process **reuses
it** rather than inventing a parallel scheme. Where it does not, the requirement
is stated as work to be done, not assumed.

| requirement | existing platform mechanism | dataset-specific work needed |
|---|---|---|
| Authorized collection only | — | Written data-use agreement naming cameras, purpose, duration, and the authorizing officer **[REVIEW REQUIRED]** |
| Access control | `app/security.py` `require_roles`, five seeded roles | Separate roles for *raw frame* access vs *plate crop* access (below) |
| Audit trail | `app/audit.py` — tamper-evident hash chain | Extend to dataset export and annotation actions |
| Retention & deletion | `settings.evidence_retention_days`, `POST /api/governance/purge-expired` (dry-run by default, requires `dry_run:false` **and** `confirm:true`) | A dataset retention clock, set by the agreement, not by us |
| Encryption at rest | **not currently provided by the platform** | Full-disk or volume encryption on the annotation host **[REVIEW REQUIRED]** |
| Encryption in transit | HTTPS/TLS at deployment; RTSP is often unencrypted | Transfer of collected footage over an encrypted channel only |
| No public exposure | `.gitignore`, `.dockerignore` exclude weights and corpora | **Hard rule: no real plate image is ever committed or uploaded** |

### Access tiers

| tier | sees | who |
|---|---|---|
| 0 — raw frames | full frames: faces, bystanders, surroundings, other vehicles | smallest possible group; named individuals under the agreement |
| 1 — plate crops | the plate region only | annotators |
| 2 — labels/manifests | text, boxes, metadata — **no imagery** | engineers, CI |

Annotators work at **tier 1**. Plate crops carry far less collateral personal
information than full frames, and there is no reason an annotator transcribing a
registration needs to see the rest of the scene. Crop export is automated from
tier 0 by an authorized operator.

### Controlled environment

- Annotation happens on agency-controlled machines or a controlled VDI.
- **No dataset leaves the controlled environment** without a written, audited
  export decision. Default answer is no.
- No cloud annotation service, no public labelling platform, no third-party
  model API is used on raw footage **[REVIEW REQUIRED]**.
- Camera URLs, credentials and internal hostnames are **stripped from every
  manifest** — the record schema deliberately carries an opaque `camera_id`
  (e.g. `C-014`), never a source URI. The platform already treats camera
  credentials as sensitive (`pipeline/sentinel_grid.py`, `egress_policy.py`).
- Logging: no plate text and no image paths in any log emitted from the dataset
  tooling at default verbosity.

### Development vs production separation

Training data lives in its own store, never in `backend/evidence_store/` or the
operational database. The test split in particular must never be reachable from
a process that could train on it.

### Explicitly NOT provided by this document

- Any statement that a given retention period, lawful basis, or collection
  practice is legally sufficient. That is for the agency's legal and privacy
  functions. **[REVIEW REQUIRED]**
- A data-subject access or erasure mechanism for the training corpus — the
  platform does not have one for operational data either
  (`docs/PRIVACY_GOVERNANCE.md` says so explicitly), and the dataset inherits
  that gap.
- Image-level redaction (face/bystander blurring) in collected frames. Not
  offered, and flagged as a likely requirement of any real agreement.

---

## 4. Minimum dataset definition

The counting units are kept distinct throughout, because conflating them is the
standard way dataset size gets inflated.

| unit | meaning |
|---|---|
| **unique plates/vehicles** | distinct registrations — **the unit that governs statistical power and the split** |
| total frames | source video frames retained |
| total plate crops | annotated plate observations; several per vehicle |
| unique camera-conditions | (camera × time-of-day × weather) cells actually covered |

**100 frames of one vehicle is one vehicle, not 100 training examples.** It is
~100 crops with heavy mutual correlation; it contributes roughly one
vehicle-worth of independent signal and exactly one unit of test-set power.

### Pilot

| | |
|---|---|
| unique plates/vehicles | **2,000** |
| plate crops | 8,000–10,000 (≈4–5 usable frames per vehicle) |
| frames retained | 15,000–25,000 (before quality filtering) |
| unique camera-conditions | ≥12 cells (≥4 cameras × ≥3 lighting conditions) |
| split | 1,200 train / 300 val / **500 test**, vehicle-disjoint |
| purpose | answer whether a plate-trained recogniser beats 0.24 at all, with enough power to detect **+7.9 pp** |

### Production target

| | |
|---|---|
| unique plates/vehicles | **15,000–20,000** |
| plate crops | 60,000–100,000 |
| unique camera-conditions | ≥40 cells |
| split | ~12,000 train / 2,000 val / **1,500–2,000 test** |
| purpose | rare-glyph coverage and per-condition evaluation with usable confidence intervals |

---

## 5. CCTV sampling design

### Target distribution (pilot, 2,000 vehicles)

Natural traffic distribution is the default; the **oversample** column marks
where deliberate over-representation is needed because the class is rare in
traffic but operationally important or known-hard.

| dimension | natural share | target share | oversample? |
|---|---|---|---|
| **Lighting** | | | |
| day | ~65% | 45% | — |
| night | ~25% | **35%** | **yes** — hardest condition, IR/glare behaviour differs completely |
| dawn/dusk | ~10% | **20%** | **yes** — mixed illumination, rare window, high error rate expected |
| rain / fog | occasional | **≥5% if obtainable** | **yes** — opportunistic; do not fabricate |
| **Vehicle type** | | | |
| car (private) | ~55% | 40% | — |
| motorcycle | ~30% | **30%** | **yes** — small two-row plates, worst-case size bucket |
| commercial / transport (yellow) | ~10% | **20%** | **yes** — different colour scheme and font conventions |
| bus / truck | ~5% | **10%** | **yes** — high mounting, oblique angles |
| **Geometry** | | | |
| front plate | | 45% | — |
| rear plate | | 55% | — |
| yaw < 15° | | 40% | — |
| yaw 15–30° | | 35% | — |
| yaw > 30° | | **25%** | **yes** — measured failure mode (`KL34A465`) |
| **Plate style** | | | |
| standard / HSRP | ~85% | 70% | — |
| non-standard (italic, handwritten, custom font) | ~10% | **20%** | **yes** — **5 of 10 Phase 2 recognition failures** |
| damaged / dirty / bolt-obscured | ~5% | **10%** | **yes** — measured (`MP07L7524`: bolt read as `S`) |
| **Registration** | | | |
| local state/UT | dominant | ≤60% | — |
| other states | | **≥35%** | **yes** — state-code diversity drives letter coverage |
| BH-series | rare | **as encountered, ≥1%** | **yes** — flag every instance; do not synthesize into the real set |

**We cannot balance the real world, and should not pretend to.** Night, rain and
BH-series arrive when they arrive. The honest approach is to record the achieved
distribution alongside the target, report both, and treat any cell below its
minimum as a stated evaluation limitation rather than quietly aggregating it
away.

---

## 6. Plate-size buckets

Anchored to **character height in pixels**, not plate width — glyph height is
what determines legibility and is what the recogniser's input resize is defined
against.

Measured distribution of the current phone-photo corpus, for contrast:

```
min 12.7   p25 41.2   median 75.4   p75 119.9   max 595.1  px glyph height
```

Bucket boundaries, kept as proposed because the current data supports them and
because they align with the recogniser's 32px input height:

| bucket | rationale | current corpus | exact match there |
|---|---|---|---|
| **< 20 px** | below the model's input height — information is genuinely absent | (within the 6 samples under 30px) | — |
| **20–30 px** | marginal; upscaling is doing real work | 6 samples total <30px | **1/6** |
| **30–50 px** | typical mid-range CCTV at a junction | 2 | 0/2 |
| **50–75 px** | comfortable | 4 | 1/4 |
| **75–100 px** | close range | 2 | 0/2 |
| **> 100 px** | near-field / phone photography | 11 | 5/11 |

Two things this table already shows, and they shape the whole plan:

1. The current corpus is **weighted toward sizes CCTV will rarely produce** —
   11 of 25 samples exceed 100px glyphs.
2. **Even at >100px, exact match is only 5/11.** Size is not what is failing,
   which independently corroborates Phase 2's conclusion.

`< 20 px` is split out from `20–30 px` deliberately: it is the bucket where a
correct answer may be information-theoretically impossible, and a model should
be allowed to *honestly decline* there rather than be scored as if it failed.

**The final benchmark must report exact match per bucket.** An aggregate number
on a corpus weighted toward large plates would not tell us whether the
recogniser works at CCTV resolution — which is the actual question.

---

## 7. Annotation specification

### Canonical record (JSON Lines, one per plate observation)

```json
{
  "record_id": "c014_20260912T081413_v0042_f003",
  "image_id": "c014_20260912T081413_f003",
  "camera_id": "C-014",
  "timestamp": "2026-09-12T08:14:13.240+05:30",
  "vehicle_id": "v0042",
  "frame_path": "frames/C-014/20260912/081413_003.jpg",
  "plate_crop_path": "crops/C-014/v0042_f003.png",
  "vehicle_bbox": [x1, y1, x2, y2],
  "plate_bbox": [x1, y1, x2, y2],
  "plate_quad": [[x,y],[x,y],[x,y],[x,y]],
  "plate_text": "GJ05AB1234",
  "plate_row_layout": "single|double",
  "vehicle_category": "car|motorcycle|bus|truck|commercial",
  "plate_face": "front|rear",
  "plate_style": "standard|hsrp|italic|handwritten|damaged|dirty|obscured",
  "visibility": "full|partial|occluded",
  "quality": "clear|marginal|unreadable",
  "label_confidence": "certain|uncertain",
  "conditions": {"time_of_day": "day|dusk|night", "weather": "clear|rain|fog"},
  "measured": {"glyph_px": 31.0, "blur_var": 142.5, "rms_contrast": 48.1, "yaw_deg": 18},
  "split": "train|val|test"
}
```

**Identifier hygiene.** `vehicle_id` is an opaque dataset-local id, **not** the
registration and not an operational `Track.id`. `camera_id` is the opaque
deployment code, never a URI or credential. The registration appears exactly
once per record, in `plate_text`, so its distribution is controllable.

### Bounding box rules

- Tight to the plate's **outer printed edge**; include the painted/embossed rim,
  exclude the mounting frame or holder.
- **Exclude** the "IND" marker column, state emblem and hologram where they sit
  outside the character field — Phase 2 shows these are read as characters
  (`INDUP8ELAE9889`).
- **Exclude** adjacent dealer/model badges (`SUCUKIDLBCD1210` came from a SUZUKI
  badge inside a loose box).
- Always record **`plate_quad`** (TL→TR→BR→BL). `plate_bbox` is derived from it.
  The quad is what makes rectification possible and is already the input shape
  `plate_preprocess.four_point_transform` consumes.

### Text rules

- Transcribe **exactly what is visually present**: uppercase, `[A-Z0-9]` only,
  no spaces or separators. Two-row plates: top row then bottom row.
- **Never correct toward a valid registration.** If the plate visually shows a
  letter `O`, the label is `O`. Correcting labels toward the regex trains the
  model on the regex and makes every downstream accuracy figure circular.
  Validation stays downstream in `anpr.looks_like_plate`.

### Edge cases

| case | rule |
|---|---|
| unreadable plate | keep the record; `plate_text: ""`, `quality: unreadable`. Needed to train the detector and to measure honest rejection |
| partially visible | transcribe **only visible characters**; `visibility: partial`, `label_confidence: uncertain`; **excluded from test** |
| occluded (wiper, tow bar, dirt) | `visibility: occluded`; transcribe what is legible |
| multiple plates in frame | one record each; never merge |
| duplicate frames | near-duplicate detection in QC (§8); keep at most N per vehicle per second |
| duplicate vehicles | same registration re-observed = **same `vehicle_id`**, same split, always |
| uncertain label | `label_confidence: uncertain` — allowed in train, **never in test** |
| damaged / dirty | annotate normally; tag `plate_style` |
| non-standard font | annotate normally; tag `italic`/`handwritten`. **Never skip** — these are 5 of 10 Phase 2 recognition failures |
| motorcycle | usually `double` layout and small; tag `vehicle_category: motorcycle` |
| extreme perspective | annotate the quad accurately; record `yaw_deg`; keep — oblique is a measured failure mode |

---

## 8. Data-leakage prevention

**Split hierarchy:**

```
plate identity (registration)        <- primary, non-negotiable key
        ↓
camera / session                     <- secondary: whole cameras held out for a
        ↓                               separate "unseen viewpoint" test slice
train / validation / test
```

Rules:

1. Split by **normalized plate text**. A vehicle seen on three cameras across
   two days is **one identity** and lands wholly in one split.
2. **No frame of a vehicle may appear in more than one split.** Consecutive
   frames are near-duplicates; splitting on frames puts the same plate, same
   lighting, same angle in both train and test, and the model scores its own
   training data.
3. A **held-out camera** slice measures viewpoint generalization separately from
   vehicle generalization.
4. Near-duplicate detection (perceptual hash) runs **within** each vehicle to cap
   redundant crops, and **across** splits as a leakage tripwire.
5. Split assignment is written into each record and committed as
   `splits/v1.json`. Every experiment loads that manifest — never re-splits.
6. **CI gate:** recompute the intersection of plate identities across splits and
   **fail the build if non-empty.** Same for the perceptual-hash cross-split
   check.

**Test-set freeze.** Once benchmarking begins, the test split is frozen: no
additions, no removals, no relabelling. A relabelling that fixes a genuine
annotation error requires a **version bump** (`v1` → `v2`) and re-running every
prior configuration, because a number from `v1` and a number from `v2` are not
comparable. Frozen test sets are what make the 0.24 baseline mean anything.

---

## 9. Character coverage

Report generated for every dataset version, per character across `0-9 A-Z`:

| char | occurrences | unique plates | train | val | test |
|---|---|---|---|---|---|

Worked against the current corpus, as the format example and as evidence of why
this report is needed:

```
ZERO-COVERAGE: I O Q V Z
<=2 occurrences: C E F H N R S T U W X Y
total instances: 231      classes present: 31/36
```

**Coverage targets:**

| tier | pilot | production |
|---|---|---|
| every class present in train | ≥1 | ≥1 |
| every class in **test** | ≥20 occurrences | ≥100 |
| common classes in train | ≥300 | ≥1,000 |
| **rare classes** (`I O Q V Z F X`) in train | ≥100 | ≥500 |

**Rare classes are filled by synthetic data (Phase 3 §12), not by duplicating
real plates.** Duplicating the same plate to balance a class teaches the model
that plate, not that glyph — it inflates the training count while adding no
visual diversity, and it risks the duplicate crossing a split boundary.

A class below its **test** minimum is reported as *not evaluated* for that
character, never silently averaged into the aggregate.

---

## 10. Licensing decision matrix

`UNKNOWN — legal review required` is the correct entry wherever a licence does
not say so explicitly. No cell is marked **Yes** on inference.

| Source | Training | Fine-tuning | Government use | Commercial use | Derivative model | Redistribution | Decision |
|---|---|---|---|---|---|---|---|
| **DataCluster public sample (current n=25)** | **No** (ND) | **No** (ND) | **No** (NC) | **No** (NC) | **No** (ND) | **No** (ND) | **Benchmark only — never train** |
| **DataCluster commercial tier** | UNKNOWN | UNKNOWN | UNKNOWN | UNKNOWN | UNKNOWN | UNKNOWN | **UNKNOWN — legal review required**; §2.B checklist must be answered in writing before purchase |
| **sanchit2843/Indian_LPR** | No | No | No | No | No | No | **Unusable** — no licence stated; data not released ("legalities involved in making Indian Road data public") |
| **Our own CCTV footage** | UNKNOWN | UNKNOWN | UNKNOWN | UNKNOWN | UNKNOWN | UNKNOWN | **UNKNOWN until a written data-use agreement exists** — but this is the source most likely to reach an unambiguous Yes |
| Scraped web / social-media images | No | No | No | No | No | No | **Prohibited** — no rights, no provenance, privacy exposure |
| **PARSeq (model code)** | Yes | Yes | Yes | Yes | Yes | Yes | Apache-2.0 — usable |
| **CRNN reference implementations** | Likely (MIT/BSD) | Likely | Likely | Likely | Likely | Likely | **Verify per repository before use** |
| **EasyOCR (incumbent)** | Yes | Yes | Yes | Yes | Yes | Yes | Apache-2.0 |
| **Ultralytics YOLOv8 (in production now)** | Yes | Yes | Yes | **AGPL-3.0 conditions apply** | Yes | AGPL-3.0 conditions | **[REVIEW REQUIRED]** — pre-existing exposure, independent of Phase 3. AGPL-3.0 requires publishing the complete corresponding source of the whole derivative work; an Enterprise Licence removes that obligation |
| Plate fonts for synthesis | per font | per font | per font | per font | per font | per font | Each font's licence recorded in `training/ASSETS.md` before use; **rendered-output redistribution rights required** |

---

## 11. Model plan — unchanged

| role | model | why |
|---|---|---|
| **Primary** | **CRNN + CTC** | lightweight; sequence recognition without explicit character segmentation; practical CPU deployment (this deployment is `torch 2.14.0+cpu`, no CUDA); straightforward training; emits a plate string directly |
| **Ceiling comparator** | **PARSeq** (Apache-2.0) | stronger transformer baseline; tells us what accuracy the lighter model is leaving on the table |

Neither is trained in M0. No additional generic OCR engine is added — TrOCR is
already measured in-repo at 0.04 exact match against EasyOCR's 0.24, and that
result rules out the class, not just the model.

---

## 12. Phase 3 success criteria (strengthened)

The statistical gate stands:

> Demonstrate a meaningful improvement over the measured **0.24** baseline on a
> **frozen, vehicle-disjoint CCTV test set** — concretely ≥ **0.319** on a
> 500-plate test set with non-overlapping 95% CIs, or McNemar p < 0.05.

**Operational reporting is now also mandatory.** A model does not pass on the
aggregate alone.

**Overall:** exact match (with CI) · CER · substitution / insertion / deletion
rates **reported separately** · **confidence calibration** (reliability curve and
expected calibration error — a recogniser reporting 0.9 on reads that are right
60% of the time is unusable for a confidence-gated pipeline, whatever its
accuracy).

**By condition:** day · night · dawn/dusk · rain/glare where available · per
camera · **per plate-size bucket (§6)** · vehicle type · front/rear ·
standard vs non-standard plate.

**System:** p50/p95 latency per plate · achievable FPS per camera · CPU
utilization · RSS · GPU memory if used · false-positive rate · **false positives
among accepted reads**.

### Blocking conditions — a model FAILS despite clearing 0.319 if

1. it is **materially worse than the current pipeline** on any reported
   condition slice with ≥100 test plates;
2. its **false-positive rate among accepted reads** exceeds the current 0.5455;
3. it is **badly calibrated** — confidence does not track correctness, breaking
   the persistence and review gates that depend on it;
4. it cannot meet the per-camera latency budget on **CPU**, unless the
   deployment separately commits to GPU inference.

An overall number that hides a collapse at night, or on motorcycles, or below
30px, is not an improvement to a police system.

---

## 13. Dataset directory structure

Images live **outside the repository**, in the controlled environment. Only
schema, tooling, manifests and checksums are ever committed.

```
# CONTROLLED ENVIRONMENT — never committed, never uploaded
/secure/anpr_dataset/
├── v1/
│   ├── frames/<camera_id>/<date>/...       tier 0 — restricted access
│   ├── crops/<camera_id>/...               tier 1 — annotator access
│   ├── records.jsonl                       tier 2 — the canonical annotations
│   ├── splits/v1.json                      frozen split manifest
│   ├── reports/
│   │   ├── qc_report.md
│   │   ├── character_coverage.md
│   │   ├── size_distribution.md
│   │   └── achieved_vs_target_distribution.md
│   ├── CHECKSUMS.sha256
│   └── DATASET_CARD.md                     provenance, authorization, terms
└── v2/ ...                                 versions are additive, never edited

# THIS REPOSITORY — tooling and specs only
training/
├── ASSETS.md                               licence record for every asset
├── dataset/{schema.py,build_manifest.py,split.py,export_crops.py}
├── qc/{checks.py,report.py}
└── evaluate/metrics.py                     reuses backend CER definitions
```

**Versioning:** a dataset version is immutable once benchmarking starts.
Corrections create `v2` with a changelog; results are never compared across
versions. `CHECKSUMS.sha256` plus the `DATASET_CARD.md` make a result
reproducible and its provenance auditable — the same standard the platform
already applies to evidence.

---

## 14. M0 exit criteria

| # | criterion | status |
|---|---|---|
| 1 | A legally usable training source | **BLOCKED** — none identified |
| 2 | Written permission / licence terms sufficient for intended use | **BLOCKED** — no agreement, no vendor terms |
| 3 | Data governance requirements identified | **DONE** (§3) |
| 4 | Annotation specification finalized | **DONE** (§7) |
| 5 | Dataset schema finalized | **DONE** (§7) |
| 6 | Vehicle-disjoint split strategy finalized | **DONE** (§8) |
| 7 | Quality-control checks defined | **DONE** (Phase 3 §9 + §8 here) |
| 8 | Character coverage requirements defined | **DONE** (§9) |
| 9 | CCTV resolution / plate-size distribution **measured** | **PARTIAL** — measured on phone photography (median 75.4px); **the CCTV distribution is unmeasured and cannot be known until footage exists** |
| 10 | Frozen test-set procedure defined | **DONE** (§8) |
| 11 | Privacy/security review requirements documented | **DONE** (§3), sign-off outstanding **[REVIEW REQUIRED]** |
| 12 | Dataset versioning strategy defined | **DONE** (§13) |

**10 of 12 complete. The two outstanding are the two that cannot be solved by
engineering.**

Criterion 9 deserves a note: it is marked PARTIAL rather than DONE because the
only distribution we have measured is the wrong one. A cheap way to close it
before any annotation begins is to capture a few hours of unlabelled footage
from the target cameras and measure the plate-size histogram alone — that needs
far less authorization than a labelled corpus and would tell us immediately
whether the deployment sits mostly in the `<30px` bucket, which would change the
model input resolution and possibly the architecture choice.

---

## 15. Prohibited actions (standing rules)

- **Do not** download datasets of unclear provenance because they contain many
  Indian plates.
- **Do not** scrape Google Images, social media, or public camera feeds.
- **Do not** train on the CC BY-NC-ND benchmark corpus without explicit written
  rights.
- **Do not** commit real plate images to this repository.
- **Do not** upload real plate data to any third-party service, annotation
  platform, or model API.
- **Do not** log plate text, frame paths, camera URIs or credentials from
  dataset tooling.
- **Do not** let the test split reach a process that can train on it.

---

## 16. M0 status

### Status: **BLOCKED**

**Missing — both require action outside engineering:**

1. **A lawfully usable training source.** The corpus in hand is CC BY-NC-ND
   (bars training, derivatives, and commercial/government use). The one
   published alternative with meaningful scale is unlicensed and unreleased.
   The commercial tier has no published terms.
2. **Written authorization to collect from the deployment's own cameras** — a
   data-use agreement naming cameras, purpose, retention, access tiers and the
   authorizing officer, plus privacy/security sign-off. **[REVIEW REQUIRED]**

Everything an engineering team can settle in advance **is settled**: schema,
annotation spec, split strategy, QC, coverage targets, size buckets, governance
design, versioning, directory structure, and the strengthened success criteria.

### Recommendations

| item | recommendation |
|---|---|
| **Data source** | **Own CCTV footage under a written agreement.** It is the only source that matches the deployment distribution, the only one that yields video for temporal fusion, and the only one whose rights can be made unambiguous in one document. Pursue the DataCluster commercial tier in parallel **only** as a supplementary source, and only if the §2.B checklist comes back clean |
| **Expected cost** | **Unknown — no published pricing.** Do not budget a figure yet. The real cost driver is annotation: ~2,000 plates × 20–30 s ≈ **15–20 hours** labelling, plus QC and 5% double-labelling. External licensing, if pursued, requires a written quote |
| **Minimum unique vehicles** | **2,000** (pilot), with **≥500 vehicle-disjoint test** |
| **Target frames** | 15,000–25,000 retained → 8,000–10,000 annotated crops |
| **Plate-size distribution** | ≥15% per bucket in `20–30`, `30–50`, `50–75` px; **do not let >75px dominate** as it does in the current corpus |
| **Character coverage** | every class ≥100 in train (≥500 for `I O Q V Z F X`, synthetic top-up permitted), ≥20 in test |
| **Annotation format** | **JSON Lines**, one record per plate observation (§7) |
| **Privacy** | three access tiers, controlled environment, no data egress without an audited written decision, no plate data in logs or in this repository |
| **Directory structure** | §13 — images outside the repo; manifests, checksums and tooling inside |

### Next M1 task — startable now, unblocked

Build the dataset tooling against the finalized specification, tested on
**synthetic placeholder records** so that not a single real plate image is
required:

```
training/dataset/schema.py        the §7 record + validation
training/dataset/split.py         identity-based split -> splits/v1.json
training/qc/checks.py             REJECT/REVIEW rules
training/qc/report.py             coverage, size-distribution, QC reports
training/evaluate/metrics.py      CER/exact match, reusing backend definitions
CI: leakage gate                  fails the build on cross-split identity overlap
```

When authorization lands, annotation starts against tooling that is already
tested — instead of the tooling being written in a hurry around whatever data
arrives.

**Do not begin model training until criteria 1 and 2 are genuinely satisfied.**
