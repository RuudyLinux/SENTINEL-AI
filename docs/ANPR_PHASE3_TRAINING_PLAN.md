# Phase 3 — Dedicated Indian license-plate recognition: training plan

**Status: PLAN ONLY. No production code changed, no model trained, no dataset
acquired. Nothing in this document is a measured result, and no accuracy target
is promised.**

This is the design that Phase 2's failure analysis points to. It is written to
be argued with before any of it is built.

---

## 1. Current baseline

Everything below is measured and reproducible (`docs/ANPR_ACCURACY.md`):

| metric | value |
|---|---|
| exact match | **0.24** |
| character error rate | **0.3983** |
| false-positive rate | **0.24** |
| false positives among accepted reads | 0.5455 |
| tests | 851 passing |
| corpus | n=25 labelled plates, mobile-phone stills |

These numbers do not change in this phase and must not change between future
experiments.

---

## 2. Phase 2 findings this plan is built on

| finding | evidence |
|---|---|
| Recognition is the dominant addressable bottleneck | 10 of 18 failures (56%) |
| Localization is **not** an accuracy lever | human-drawn plate boxes give exact match 0.28 — identical to reading the whole crop. Recovers 3 samples, breaks 3, net **0.00** |
| Crops are readable | 22/25 (88%) readable by a human; failures concentrate in *crisp* images |
| Post-processing cannot fix it | only 5 of 25 substitutions are the six classic confusions; **32 of 57 edits are insertions/deletions**, i.e. segmentation |
| Preprocessing cannot fix it | no variant beats `original`; 64% of samples unfixable by any variant |
| Validation is not at fault | 0 failures attributable to the gate |
| Non-standard plates are a real class | 5 of 10 recognition failures: handwritten, italic/script, mounting-bolt-as-character |

The single most informative sample: `WB42AX7446` — 113px glyphs, contrast 54.7,
front-on, standard font, perfect box — read as `HBZZAX7L46`.

---

## 3. The binding constraint is DATA, not architecture

This needs stating before anything else, because it inverts the usual order of
work.

Every candidate architecture is available under a permissive licence and is
straightforward to train. **There is no lawfully usable Indian plate-recognition
training corpus in this project's possession, and none was found that could be
acquired off the shelf.**

| source | licence | usable for training here? |
|---|---|---|
| [DataCluster Labs Indian Number Plates](https://huggingface.co/datasets/Dataclusterlabspvtltd/indian-number-plates-dataset) (the current n=25 benchmark corpus) | **CC BY-NC-ND 4.0** | **No.** NonCommercial bars police/government deployment; NoDerivatives bars producing a trained model from it. Usable as a *benchmark* only, which is how it is used today. |
| [sanchit2843/Indian_LPR](https://github.com/sanchit2843/Indian_LPR) (16,192 images, 21,683 plates) | **No licence stated; dataset not released** — "We can't make dataset public because of legalities involved in making Indian Road data public" | **No.** Not obtainable, and unlicensed code. |
| DataCluster full commercial set (15,000+ images) | commercial licence, paid | Possibly — requires a purchase and a licence review. |

**Consequence:** the first milestone of Phase 3 is not training. It is acquiring
a corpus that can be lawfully trained on and lawfully deployed by a police
agency. Everything downstream is blocked on that, and no amount of model work
substitutes for it.

The realistic source is **the deployment's own cameras**, with the operating
authority's permission — which also produces exactly the distribution the model
will face, and produces *video*, which is the only way temporal fusion ever gets
measured.

---

## 4. The current corpus cannot validate Phase 3

A second blocking fact, computed rather than asserted. 95% Wilson confidence
intervals on exact match at p=0.24:

| test-set size | 95% CI | width |
|---|---|---|
| **n=25 (today)** | **[0.115, 0.434]** | ±16.0 pp |
| n=100 | [0.167, 0.332] | ±8.3 pp |
| n=500 | [0.205, 0.279] | ±3.7 pp |
| n=1000 | [0.215, 0.267] | ±2.6 pp |

Smallest true improvement detectable against the 0.24 baseline (two-proportion,
95% confidence, 80% power):

| test-set size | new model must score at least |
|---|---|
| **n=25 (today)** | **0.604** (+36.4 pp) |
| n=100 | 0.423 (+18.3 pp) |
| n=500 | **0.319** (+7.9 pp) |
| n=1000 | 0.295 (+5.5 pp) |
| n=2000 | 0.279 (+3.9 pp) |

**On the current corpus, a genuinely better model scoring 0.40 would be
statistically indistinguishable from the 0.24 baseline.** This is why Phase 2
refused to call 0.28 an improvement, and it sets a hard floor on the test set:
**at least 500 vehicle-disjoint plates**, ideally 1,000.

Character coverage is the same story. The 25 labels contain 231 character
instances, cover 31 of 36 classes, and **never contain `I`, `O`, `Q`, `V`, `Z`**
— five classes with zero support, three more (`S`, `W`, `Y`) appearing once.

---

## 5. Dataset definition

### 5.1 Record schema

One record per annotated plate observation:

```json
{
  "record_id": "cam07_20260912T081413_trk42_f003",
  "frame_path": "frames/cam07/20260912/081413_003.jpg",
  "camera_id": "C-014",
  "track_id": 42,
  "timestamp": "2026-09-12T08:14:13.240+05:30",
  "vehicle_bbox": [x1, y1, x2, y2],
  "vehicle_class": "car",
  "plate_bbox": [x1, y1, x2, y2],
  "plate_quad": [[x,y],[x,y],[x,y],[x,y]],
  "plate_crop_path": "crops/cam07/.../trk42_f003.png",
  "plate_text": "GJ05AB1234",
  "plate_row_layout": "single|double",
  "vehicle_category": "private|commercial|motorcycle|transport",
  "plate_style": "standard|hsrp|italic|handwritten|damaged|dirty|obscured",
  "legibility": "clear|marginal|unreadable",
  "label_confidence": "certain|uncertain",
  "conditions": {"time_of_day": "day|dusk|night", "weather": "clear|rain|fog"},
  "quality": {"glyph_px": 31.0, "blur_var": 142.5, "rms_contrast": 48.1,
              "occlusion_pct": 0, "yaw_deg": 18}
}
```

`camera_id`, `track_id` and `timestamp` are carried because the same schema must
later support the temporal-fusion benchmark (§12). Without `track_id` captured at
annotation time, a video benchmark cannot be constructed retrospectively.

### 5.2 Recognition training input

**`plate crop → ground-truth string`.** Not the vehicle crop.

Justification from our own data rather than convention: Phase 2 measured that a
human-drawn plate box gives the same exact match as the whole vehicle crop
(0.28 vs 0.28), so the vehicle context carries no information the recogniser
needs — but it does carry the badge text, sticker text and "IND" markers that
produced `SUCUKIDLBCD1210` and `INDUP8ELAE9889`. Training on vehicle crops would
make the model responsible for localization *and* recognition, doubling what it
must learn from a corpus that is the scarce resource.

The crop is exported with a small, fixed proportional margin (§7.1) so the model
sees plate borders consistently rather than learning an artefact of tight
cropping.

---

## 6. Dataset size, reasoned

The usual "500 pilot / 5,000 production" numbers are in the right order of
magnitude but are not derived from anything. Here is the derivation.

**The binding requirement is per-character-class support in the tail, not plate
count.** A CTC recogniser learns 36 glyph classes. Character frequency in Indian
registrations is strongly non-uniform: state codes concentrate `A B D G H J K L
M N P R S T U W`, while `F Q V X Z` and `I O` (rare by design, to avoid
1/0 confusion) appear in a small minority of plates.

Working assumptions, to be replaced by a real frequency count once a corpus
exists:

- ~10 characters per plate.
- A rare class appears in roughly 1–3% of plates.
- Robust learning of a glyph across fonts and conditions needs on the order of
  **500–1,000 instances of that glyph**.

That gives:

| target | unique plates | rationale |
|---|---|---|
| common classes ≥ 1,000 instances | ~2,000 | 2,000 × 10 = 20,000 char instances; frequent glyphs comfortably covered |
| rare classes ≥ 500 instances | **~15,000–20,000** | a glyph at 2% frequency needs ~25,000 plates to reach 500 naturally |

Synthetic data (§11) exists precisely to fill the rare-class tail without
demanding a 20,000-plate real corpus.

### Minimum viable dataset (pilot)

| | |
|---|---|
| unique vehicles/plates | **2,000** |
| crops | 8,000–10,000 (3–5 frames per vehicle, from video) |
| split | 1,200 train / 300 val / **500 test** |
| purpose | answer "does a plate-trained recogniser beat 0.24 at all", with a test set that can detect a +7.9 pp improvement |
| known limitation | rare glyphs under-supported; must be topped up synthetically and reported as such |

### Ideal dataset (production)

| | |
|---|---|
| unique vehicles/plates | **15,000–20,000** |
| crops | 60,000–100,000 |
| split | ~12,000 train / 2,000 val / **1,500–2,000 test** |
| purpose | tail glyph coverage, and enough per-slice volume that each condition can be *evaluated separately* |

### Per-slice minimums (the part usually forgotten)

A slice you cannot evaluate is a slice you cannot claim. Each of these needs
**≥300 test plates** to carry a usable CI, in addition to its training share:

night · dusk · motorcycle · commercial/transport · oblique (yaw > 30°) ·
small plate (glyph < 20px) · motion-blurred · non-standard font (italic,
handwritten) · damaged/dirty · rain/fog.

Ten slices × 300 = 3,000 test plates for a fully sliceable evaluation. That is
the honest cost of per-condition claims; the pilot deliberately does not attempt
it and will report aggregate numbers only.

---

## 7. Annotation specification

### 7.1 Plate bounding box

- **Tight to the plate's outer printed edge**, excluding the mounting frame,
  bezel, or surrounding bodywork.
- **Include the plate border** (the painted/embossed rim) but not a decorative
  holder.
- **Exclude** the "IND" country marker column and any state emblem/hologram
  where they sit outside the character field — Phase 2 shows these get read as
  characters (`INDUP8ELAE9889`).
- **Exclude** dealer badges and model badges adjacent to the plate
  (`SUCUKIDLBCD1210` came from a SUZUKI badge inside a loose box).
- For perspective, also record **`plate_quad`**: the four corners in
  top-left → top-right → bottom-right → bottom-left order. The axis-aligned
  `plate_bbox` is derived from it. The quad is what makes rectification
  possible and is already what `plate_preprocess.four_point_transform` consumes.

### 7.2 Text

- Transcribe **exactly what is visually present**, uppercase, alphanumerics
  only. No spaces, hyphens, dots.
- **Do not "correct" to a valid registration.** If the plate visually reads
  `GJO5AB1234` with a letter O, that is the label. The recogniser is being
  taught to read glyphs; validation is a separate downstream layer
  (`anpr.looks_like_plate`) and must stay that way. Correcting labels toward
  the regex would train the model on the regex and make every downstream
  accuracy figure circular.
- Two-row plates: transcribe **top row then bottom row**, no separator.

### 7.3 Edge cases

| case | rule |
|---|---|
| partially visible plate | annotate the box; set `legibility` and transcribe **only the characters actually visible**; set `label_confidence: uncertain` |
| unreadable plate | annotate the box, `plate_text: ""`, `legibility: unreadable`. **Keep it** — it trains the detector and is needed to measure honest rejection |
| multiple plates in frame | one record each; never merge |
| motorcycle | usually two-row and small; `vehicle_category: motorcycle`, `plate_row_layout: double` |
| non-standard font | annotate normally, tag `plate_style` (`italic`, `handwritten`) — never skip; these are 5 of 10 Phase 2 recognition failures |
| mounting bolt over a character | transcribe the character if a human can infer it, else treat as partially visible; tag `plate_style: obscured` |
| annotator unsure of any character | `label_confidence: uncertain` — these are **excluded from the test set** and may be kept in training |

### 7.4 Format

**JSON Lines (`.jsonl`), one record per line**, plus the crop images on disk.

Chosen over COCO and YOLO because:

- the record carries `plate_text`, `track_id`, `camera_id`, `timestamp` and
  quality metadata that neither COCO nor YOLO has a place for;
- recognition training reads `crop_path → text`, which is a flat table, not a
  detection format;
- it streams, diffs and greps, and one bad line does not invalidate the file.

A **derived** YOLO-format export is generated for any future *detector*
training, from the same source of truth. COCO is not used: nothing in this
project consumes it.

---

## 8. Data split — vehicle-disjoint, non-negotiable

**The split key is the plate identity, not the frame, not the track.**

```
CORRECT                           WRONG (fabricates accuracy)
vehicle A (all frames) → train    vehicle A frame 1 → train
vehicle B (all frames) → val      vehicle A frame 2 → val
vehicle C (all frames) → test     vehicle A frame 3 → test
```

Consecutive frames of one vehicle are near-duplicates. Splitting on frames puts
an image of the *same plate under the same lighting at the same angle* in both
train and test, and the model scores its own training data. This is the single
most common way an ANPR accuracy number becomes fiction.

Rules:

1. Split by **normalized plate text**. A vehicle that appears on three cameras
   on two days is *one* identity and lands wholly in one split.
2. Where the same plate is genuinely re-observed later, it still follows its
   identity. Do not treat a second visit as a new vehicle.
3. **Additionally hold out whole cameras** for a secondary test slice, so
   generalization to an unseen viewpoint is measurable separately from
   generalization to an unseen vehicle.
4. Split assignment is written into the record and **committed as a manifest**
   (`splits/v1.json`), so every experiment uses byte-identical splits.
5. A CI check recomputes the intersection of plate identities across splits and
   **fails the build if it is non-empty** (§9).

---

## 9. Quality-control pipeline

Automated checks run over the corpus before any training. Each emits either
`REJECT` (structurally broken) or `REVIEW` (suspicious, needs a human).

**REJECT — structurally invalid:**

| check | rule |
|---|---|
| missing bbox | no `plate_bbox` |
| bbox outside image | any coordinate outside frame bounds |
| degenerate bbox | width or height ≤ 0 |
| invalid characters | anything outside `[A-Z0-9]` after normalization |
| split leakage | a plate identity present in more than one split |
| missing crop | `plate_crop_path` does not resolve |

**REVIEW — flag for a human, never auto-drop:**

| check | rule |
|---|---|
| empty text | `plate_text == ""` and `legibility != "unreadable"` |
| unusual length | outside 6–11 characters |
| **fails the Indian format regex** | `looks_like_plate()` is false |
| unknown state code | first two characters not in `INDIAN_STATE_CODES` |
| tiny crop | glyph height < 8px |
| extreme aspect | plate aspect outside 1.5–9.0 |
| near-duplicate frame | perceptual hash within threshold of another crop of the same track |
| duplicate identity | same plate text with materially different `plate_quad` geometry across records |
| annotator disagreement | double-labelled sample where transcriptions differ |

**The format-regex check is REVIEW, never REJECT.** Auto-dropping plates that
fail the regex would delete exactly the non-standard plates that are 5 of 10
Phase 2 recognition failures, and would quietly train the model only on plates
it already handles — inflating every subsequent number. The two known-invalid
labels in the current corpus (`KL34F`, `KL498262`) are real plates and belong in
the data.

**Double-labelling:** a random 5% of the corpus is labelled twice by different
annotators; inter-annotator exact-match agreement is reported alongside model
accuracy. If annotators agree only 95% of the time, no model result above ~95%
means anything, and we need to know that number before trusting any result.

---

## 10. Candidate architectures

Evaluated against this deployment's actual constraints: **CPU-only inference**
(`torch 2.14.0+cpu`, `cuda_available False`), per-camera worker loop where OCR is
already the most expensive operation, FastAPI service in a `python:3.11-slim`
container, and a police-context licensing requirement.

| | **A. CRNN + CTC** | **B. PARSeq / ViT OCR** | **C. TrOCR-style** | **D. Char detect + classify** |
|---|---|---|---|---|
| accuracy potential on plates | high | highest | **measured worst here** | moderate |
| segmentation-free | **yes** | yes | yes | **no** |
| params / size | ~8–10M, <15MB (≈4MB int8) | ~23M+ | 300M+ | varies |
| CPU latency (est., 32×128 input) | **~5–15 ms** | ~40–90 ms | ~500 ms+ | 2 passes |
| training data needed | **moderate** | high | very high | high + char-level boxes |
| Indian plate suitability | strong — short fixed-charset strings | strong | **poor** | weak |
| blur/perspective robustness | good with augmentation | best | moderate | poor — segmentation fails first |
| ONNX export | **clean, mature** | supported | heavy | fragmented |
| licence | **MIT/BSD** (reference impls) | **Apache-2.0** ([PARSeq](https://github.com/baudm/parseq/blob/main/LICENSE)) | MIT | n/a |
| training complexity | **low** | moderate | high | high |

**TrOCR is excluded on measured evidence, not opinion.** `docs/ANPR_ACCURACY.md`
records it at exact match 0.04 against EasyOCR's 0.24 on this corpus, reading
`DL3CD1210 → DISCARD1220` and `KA01AJ7533 → TAX`. Its language-model decoder is
trained on prose; a registration is precisely not a word, and the prior that
makes it strong on documents destroys it here. This rules out the whole class of
"swap in a stronger general-purpose OCR".

**Option D is excluded on Phase 2 evidence.** 32 of 57 character edits are
insertions and deletions — the recogniser is failing at *segmentation*. An
architecture whose first stage is explicit character segmentation inherits that
failure mode and adds a character-level annotation burden. Requirement §9 of the
brief is met by CTC and by attention decoders, both of which learn
whole-image → sequence without per-character boxes.

### Recommendation: **CRNN + CTC as the production candidate, PARSeq as the ceiling comparator**

Train both on the same data and the same splits. CRNN is what ships if it is
good enough; PARSeq tells us what accuracy is being left on the table for the
extra compute. If PARSeq is dramatically better and the CPU budget cannot take
it, that is a hardware conversation with a number attached rather than a guess.

Rationale for CRNN as the default:

1. **CPU-only is a hard constraint today.** A ~10 ms recogniser fits the
   existing loop; a 500 ms one does not.
2. **Plates are short, fixed-charset, non-linguistic** — the regime CTC handles
   well and where a language model actively hurts.
3. **Segmentation-free**, directly addressing the measured insertion/deletion
   failure.
4. **Smallest data appetite** of the credible options, which matters when data
   is the binding constraint (§3).

---

## 11. Training strategy

| | |
|---|---|
| input | grayscale, height 32, width 128 (single-row); **two-row plates rectified and split into two lines, then concatenated left-to-right** before resize |
| aspect handling | resize height to 32, preserve aspect, pad/truncate width to 128; never squash — squashing distorts glyph shape, which is the signal |
| vocabulary | 36 classes `A–Z 0–9` + CTC blank. **No separator, no padding class.** |
| loss | CTC |
| optimizer | AdamW, weight decay 0.01 |
| LR schedule | 1e-3 with cosine decay, 2-epoch linear warmup |
| batch | 128 (GPU) / 32 (CPU) |
| epochs | up to 150 with early stopping |
| early stopping | patience 15 epochs on **validation exact match** (not loss — CTC loss and exact match decouple) |
| checkpointing | best-on-val-exact-match + last; both retained with their config hash |
| decoding | greedy CTC for production; beam search evaluated offline as a separate row |
| seeds | 3 seeds per configuration; report mean and spread, never a single lucky run |

### Augmentation — calibrated to measured CCTV conditions, not to look robust

Every augmentation below exists because Phase 2 or the deployment context
observed the corresponding degradation. Ranges are anchored to the corpus's own
measured statistics where available.

| augmentation | range | why |
|---|---|---|
| perspective warp | yaw ±35°, pitch ±20° | oblique plates are a measured failure (`KL34A465` #2) |
| rotation | ±8° | mounting tilt |
| motion blur | kernel 3–15px, directional | CCTV + moving vehicles; the target deployment's dominant expected degradation |
| defocus blur | Gaussian σ 0.5–2.5 | measured: `MH18AA1002`, `KL34F` |
| downscale-then-upscale | to 12–30px glyph height | **the most important one for CCTV transfer** — the corpus median is 75px glyphs, the deployment will be far smaller |
| brightness/contrast | ±40% brightness, 0.5–1.6× contrast | measured contrast range 9.1–79.1 |
| JPEG compression | quality 30–90 | RTSP/H.264 artefacts |
| sensor noise | Gaussian σ 2–12 | night gain |
| glare/specular patch | random bright ellipse, ≤25% of area | measured: `KL35H5834` |
| shadow band | random darkened half | overhang/partial lighting |
| occlusion patch | ≤15% of plate area | mounting bolts, dirt |

**Deliberately excluded:** elastic/wave distortion, colour-channel shuffling,
cutout of >25%, and vertical flips. None corresponds to anything a real plate
does. Including them inflates robustness metrics without improving real reads.

**Augmentation is applied to training only**, never to validation or test.

---

## 12. Synthetic data

**Recommended as a supplement, specifically to fill the rare-glyph tail
(§6) — not as a substitute for real data.**

The case for it here is narrow and concrete: five character classes (`I O Q V Z`)
have zero instances in the current corpus and will remain rare in any naturally
collected one. Synthesis is the only economical way to reach 500 instances of a
glyph that appears in 1% of plates.

Generator design:

```
plate layout (single-row / two-row / motorcycle)
      ↓  IND marker, state emblem, hologram placement
font (standard Charles Wright analogue, italic, handwritten-like)
      ↓  correct character grammar per layout
render → perspective warp → lighting/shadow → blur → downscale → JPEG → noise
```

**Licensing is the gate.** Fonts must be verified as permitting commercial
redistribution of *rendered output* (SIL OFL and Apache-licensed fonts do;
several "free for personal use" plate fonts do not). No font is committed until
its licence is recorded in `training/ASSETS.md`.

**How it must be measured.** Three runs on the identical test set:

1. real only
2. real + synthetic (rare-glyph-balanced)
3. synthetic only — as a *diagnostic*, to reveal the sim-to-real gap

If (2) does not beat (1) on the real test set, synthetic data is dropped.
No claim about synthetic data is made in advance.

---

## 13. Temporal fusion — design only, not implemented, not claimed

The consensus machinery already exists and is unit-tested
(`plate_tracker.Consensus`, `has_consensus`, `PLATE_MIN_OBSERVATIONS`). What
does not exist is any evidence that it helps, because a still-image corpus has
no tracks.

Planned interaction (unchanged from what is already built):

```
track 42
  frame 1 → GJ05AB1234 0.71
  frame 2 → GJ05AB1284 0.68
  frame 3 → GJ05AB1234 0.83
  frame 4 → GJ05AB1234 0.79
  frame 5 → GJ05AB1234 0.81
                ↓
      GJ05AB1234, 4/5 observations, peak 0.83
```

**Evaluation, once a video benchmark exists:**

- Metric is **per-vehicle exact match** (did the track end with the right
  plate), reported alongside per-frame exact match. These are different
  questions and must not be conflated.
- Report as a function of observation count (1, 2, 3, 5, 10+ frames), so the
  benefit curve is visible rather than a single number.
- Sweep `PLATE_MIN_OBSERVATIONS` ∈ {1,2,3,5} and report the accuracy/latency
  trade: higher thresholds mean later first-identification, which matters
  operationally for a fast-moving vehicle.
- Report **false-positive rate per vehicle**, not just accuracy. The gate exists
  to suppress wrong identities; a fusion setting that raises accuracy while
  raising false identities is not an improvement.

**No claim is made that temporal fusion improves accuracy.** It is a reasoned
expectation with zero supporting measurement.

---

## 14. Evaluation protocol

### Recognition (on vehicle-disjoint test set)

exact match · character accuracy · CER · **insertion / deletion / substitution
rates reported separately** (Phase 2 showed the mix matters: 25/20/12) ·
per-character-class confusion matrix · exact match with 95% Wilson CI.

### Detection

plate recall at IoU ≥ 0.5 · precision · IoU distribution (not just the mean —
the current detector's mean 0.136 hides that 12 of 16 detections were on
non-plates).

### System (end to end)

end-to-end exact match · false-positive rate · **false positives among accepted
reads** (the operationally important one; currently 0.5455) · p50/p95 latency
per plate · achievable FPS per camera · CPU utilization · RSS · GPU memory if
applicable.

### Mandatory slice breakdown

day/night · plate size (glyph px bands) · camera · vehicle type · yaw angle ·
blur level · standard vs non-standard plate. A slice with fewer than 100 test
plates is reported **with its count and CI**, or not reported.

### Comparison protocol

Three configurations, one frozen test set, never changed between runs:

```
1. current EasyOCR pipeline        (the 0.24 / 0.3983 baseline)
2. dedicated recognizer
3. dedicated recognizer + temporal fusion   (video benchmark only)
```

**Acceptance rule.** A result is reported as an improvement only if the 95% CIs
do not overlap, or a paired test (McNemar on per-sample correctness) reaches
p < 0.05. On a 500-plate test set that means the new model must reach **≥ 0.319**
to be called better than 0.24. A result of 0.26 is reported as "not
distinguishable from baseline" regardless of how much work produced it.

---

## 15. Deployment architecture

Target pipeline (unchanged from what exists; only the recogniser box is new):

```
YOLO vehicle detector → plate detector → plate crop
   → DEDICATED RECOGNIZER → candidate → Indian validation
   → ByteTrack temporal consensus → persistence gate
```

### Interface

The existing seam is already the right one and does not need redesigning.
`anpr.get_reader()` is a single `lru_cache`d factory, and
`anpr.read_candidate(crop, variant) -> OcrCandidate` is the only place the engine
is called. A recogniser plugs in behind a protocol:

```python
class Recognizer(Protocol):
    def read(self, plate_crop: np.ndarray) -> RecognizerResult: ...

@dataclass(frozen=True)
class RecognizerResult:
    text: str                      # raw visual transcription, NOT validated
    confidence: float              # engine's own number, never adjusted
    char_confidences: tuple[float, ...]   # per-position, when available
    latency_ms: float
    model_name: str
    model_version: str
```

Three properties this must preserve, all of them load-bearing today:

1. **`text` is the visual string, unvalidated.** Validation stays in
   `looks_like_plate`, downstream and separate. The recogniser must never be
   made regex-aware — that is how a benchmark becomes circular.
2. **`confidence` is the model's own output**, never blended with agreement or
   corroboration. The separation of `ocr_confidence` / `variants_agreeing` /
   `temporal_observations` is an existing, tested invariant.
3. **Selection by `ENGINE` setting**, defaulting to EasyOCR, so the new model
   ships dark and is switched on per deployment — same escape-hatch convention
   as `PLATE_PIPELINE_V2`.

**Not implemented in this phase.** Listed so the training output has a known
target shape.

### Model versioning

The repo already has the convention: `settings.model_version`
(`"yolov8n-coco-1.0"`) and `evidence.model_version`. Extend rather than invent —
add `recognizer_name` / `recognizer_version` to settings, and carry them onto the
`Plate` row alongside the existing `ocr_variant` / `variants_agreeing` /
`corroborated` explainability columns, via the same additive
`ensure_columns` + Alembic pattern.

Rationale: a future model will change recognition results, and a sighting whose
model is unknown cannot be re-audited or re-processed. **No schema change is made
in this phase.**

---

## 16. Hardware requirements

No GPU is present today (`torch 2.14.0+cpu`, `cuda_available False`, 16 CPU
cores). Estimates are for the CRNN candidate on ~50k crops at 32×128 and are
**engineering estimates, not measurements**.

| tier | hardware | feasibility |
|---|---|---|
| **Minimum** | CPU only, 16 cores, 16GB RAM, 50GB storage | Feasible for the 2,000-plate pilot. Expect **12–30 h per training run**, which makes hyperparameter iteration impractical. Adequate to answer "does this approach work at all". |
| **Recommended** | 1× RTX 3060 12GB / T4 / A10, 32GB RAM, 250GB SSD | **1–3 h per run.** Enables 3-seed runs and real ablations. This is the tier that makes Phase 3 a normal engineering loop rather than an endurance test. |
| **Large-scale** | 1× A100 40GB or 2× 4090, 64GB RAM, 1TB NVMe | Needed only for PARSeq at the 100k-crop scale, or synthetic pretraining. Rentable by the hour; no purchase justified yet. |

**Inference** stays CPU: CRNN int8 ONNX is expected in the ~5–15 ms range per
plate, against EasyOCR's current ~0.5–2 s per image. If that holds, the
recogniser stops being the pipeline's cost centre — but it is an estimate and
will be measured, not assumed.

---

## 17. Licensing

Treated as a gating requirement, not a footnote, because the deployment context
is a police agency.

| asset | licence | verdict |
|---|---|---|
| **Current benchmark corpus** (DataCluster) | CC BY-NC-ND 4.0 | **Benchmark only.** NonCommercial + NoDerivatives bar both deployment and training. Already the documented status quo. |
| sanchit2843/Indian_LPR | none stated, data unreleased | unusable |
| PARSeq | Apache-2.0 (ABINet BSD, CRNN MIT within it) | usable |
| CRNN reference implementations | MIT / BSD typically — **verify per repo before use** | usable with verification |
| TrOCR | MIT | excluded on measured performance, not licence |
| EasyOCR (incumbent) | Apache-2.0 | fine |
| **Ultralytics YOLOv8 (already in production here)** | **AGPL-3.0** | **Pre-existing exposure that should be reviewed independently of Phase 3.** AGPL-3.0 requires releasing the complete corresponding source of the whole derivative work; Ultralytics sells an Enterprise Licence for use without that obligation. This affects the *current* system, not just future work. |
| plate fonts for synthesis | varies; many are "free for personal use" only | each font's licence recorded in `training/ASSETS.md` before use; rendered-output redistribution rights required |

**Rules for this phase:** no asset is downloaded or committed without its licence
recorded. No model is trained on CC BY-NC-ND data. Any corpus collected from the
deployment's own cameras needs a written data-use agreement with the operating
authority, covering retention, and the privacy posture already documented in
`docs/PRIVACY_GOVERNANCE.md`.

---

## 18. Proposed project structure

Adapted to the existing repository rather than imposed on it. Training lives
**outside `backend/`** so its heavy dependencies never enter the FastAPI image —
the Dockerfile already calls out torch as a multi-minute install, and the
service must not also carry a training stack.

```
training/                          # NEW, top-level, its own requirements.txt
├── README.md
├── ASSETS.md                      # licence record for every font/model/dataset
├── requirements.txt
├── configs/                       # one YAML per experiment; hashed into checkpoints
├── dataset/
│   ├── schema.py                  # the §5.1 record, validated
│   ├── build_manifest.py          # frames+annotations -> records.jsonl
│   ├── split.py                   # vehicle-disjoint split -> splits/v1.json
│   └── export_crops.py            # plate crops with the standard margin
├── qc/
│   ├── checks.py                  # §9 REJECT/REVIEW rules
│   └── report.py                  # QC report + leakage gate
├── synth/                         # §12 generator (fonts NOT committed)
├── augment/                       # §11 transforms
├── models/
│   ├── crnn.py
│   └── parseq_adapter.py
├── train/
├── evaluate/
│   ├── metrics.py                 # reuses backend CER/levenshtein definitions
│   └── report.py                  # slice breakdowns + Wilson CIs
└── export/                        # ONNX + int8 quantization

backend/tools/anpr_bench.py        # EXISTING — stays the single source of truth
                                   # for baseline numbers; extended, not replaced
docs/ANPR_PHASE3_TRAINING_PLAN.md  # this document
```

**Reuse, not reimplementation:** `evaluate/metrics.py` must import or mirror the
exact CER/Levenshtein and normalization functions the backend benchmark already
uses. Two definitions of CER is how before/after comparisons quietly stop being
comparable.

---

## 19. Milestones, risks, expected outcomes

### Milestones

| # | milestone | exit criterion | blocked by |
|---|---|---|---|
| **M0** | **Data-use agreement + licensing review** | written permission to collect and train on deployment footage; AGPL exposure reviewed | — |
| M1 | Capture + annotation tooling | `records.jsonl` schema, QC checks, split tool, leakage gate all tested | M0 |
| M2 | **Pilot corpus: 2,000 plates** | QC clean; ≥500-plate vehicle-disjoint test set; inter-annotator agreement measured | M1 |
| M3 | CRNN baseline trained | reproducible run, 3 seeds, evaluated on the frozen test set | M2 |
| M4 | PARSeq comparator | same data, same splits | M2 |
| M5 | Synthetic ablation | real vs real+synthetic on the same test set | M3 |
| M6 | Decision gate | does the best model clear **≥0.319** on the 500-plate test set? | M3–M5 |
| M7 | Video benchmark + temporal fusion measurement | per-vehicle exact match, observation-count curve | M2 (video capture) |
| M8 | Production integration behind `ANPR_ENGINE` | dark-shipped, off by default | M6 |

**M6 is a real gate.** If the answer is no, the recommendation will be to stop
and reconsider — not to keep tuning until a number appears.

### Risks

| risk | severity | mitigation |
|---|---|---|
| **No lawfully usable corpus is obtained** | **critical — blocks everything** | M0 first; treat data acquisition as the project, not the preamble |
| Annotation quality caps the ceiling | high | 5% double-labelling; report inter-annotator agreement *before* model results |
| Split leakage fabricates accuracy | high | identity-based split + automated leakage gate that fails the build |
| Pilot corpus too small for the tail glyphs | medium | synthetic top-up, with the real-vs-real+synthetic ablation to prove it helps |
| CPU-only makes iteration impractical | medium | budget a single mid-range GPU; it converts 20h runs into 2h runs |
| Corpus is phone-quality, deployment is CCTV | **high** | downscale augmentation; collect the corpus from the *actual cameras* |
| Model beats baseline on stills, fails on video | medium | M7 video benchmark before any accuracy claim |
| AGPL-3.0 exposure via Ultralytics | medium, pre-existing | independent legal review; unaffected by Phase 3's outcome |

### Expected measurable outcomes

Deliberately stated as *what will be known*, not as numbers achieved:

1. A vehicle-disjoint test set large enough that a real improvement is
   statistically visible (currently impossible: the n=25 corpus requires 0.604
   to detect a difference).
2. A measured comparison of EasyOCR vs CRNN vs PARSeq on identical data.
3. A measured answer on whether synthetic data helps.
4. The first real measurement of temporal fusion, which has been built and
   untested since Phase 1.
5. A per-slice error profile, so the next bottleneck is identified by evidence,
   as Phase 2 did for this one.

---

## 20. Final recommendation

| question | answer |
|---|---|
| **Dataset size** | Pilot **2,000 unique plates** (~8–10k crops), of which **≥500 vehicle-disjoint test**. Production **15,000–20,000 plates**. Driven by rare-glyph coverage and by the CI arithmetic in §4, not by convention. |
| **Annotation format** | **JSON Lines** record per plate (§5.1, §7.4) + crops on disk; YOLO export derived for detector work. Not COCO. |
| **Architecture** | **CRNN + CTC** as the production candidate; **PARSeq (Apache-2.0)** trained alongside as the accuracy-ceiling comparator. TrOCR excluded on measured evidence (0.04); per-character segmentation excluded on Phase 2's insertion/deletion evidence. |
| **Training strategy** | Grayscale 32×128, 36-class vocabulary + CTC blank, AdamW + cosine, early stopping on **validation exact match**, 3 seeds, frozen committed splits. |
| **Hardware** | Pilot runs on the existing 16-core CPU. **One mid-range GPU (12GB) is strongly recommended** — it is the difference between 20-hour and 2-hour runs. |
| **Augmentation** | Perspective, motion blur, defocus, **downscale-to-CCTV-resolution**, brightness/contrast, JPEG, noise, glare, shadow, occlusion. Explicitly no elastic warp, no channel shuffle, no flips. |
| **Evaluation** | Frozen vehicle-disjoint test set; exact match with 95% Wilson CI; insertion/deletion/substitution reported separately; mandatory slice breakdown; **McNemar paired test** for the baseline comparison. |
| **Engineering effort** | M0 licensing/agreement: **unknown, external, and on the critical path**. M1 tooling ~1–2 weeks. M2 annotation ~2,000 plates at 20–30 s ≈ **15–20 h labelling** plus QC and double-labelling. M3–M5 training/ablation ~2–3 weeks with a GPU, longer on CPU. M7 video benchmark ~1–2 weeks. **Excluding M0: roughly 6–9 weeks of engineering.** |
| **Biggest risks** | 1) No lawfully usable dataset — blocks everything. 2) Annotation quality silently capping the ceiling. 3) Split leakage fabricating accuracy. 4) Phone-quality training data not transferring to CCTV. |

### The only acceptable success criterion

> Demonstrate a statistically and operationally meaningful improvement over the
> measured **0.24** exact-match baseline, on a representative,
> **vehicle-separated** CCTV test set.

Concretely, on a 500-plate test set that means **≥ 0.319** with non-overlapping
95% CIs, or a McNemar p < 0.05. **No target of 80%, 90% or 95% is offered, and
no post-training accuracy is predicted**, because nothing measured so far
supports one.

---

## 21. Roadmap — what to build first, once this plan is approved

```
1.  M0  Data-use agreement + licensing review          <- START HERE; blocks all
        (no code)

2.  M1  training/ skeleton, schema, QC, split tooling
        - training/dataset/schema.py       record + validation
        - training/dataset/split.py        identity split + manifest
        - training/qc/checks.py            REJECT/REVIEW rules
        - leakage gate wired into CI       fails the build on overlap
        - training/evaluate/metrics.py     reusing backend definitions

3.  M2  Capture + annotate the 2,000-plate pilot corpus
        - ≥500 vehicle-disjoint test plates
        - 5% double-labelled; report inter-annotator agreement FIRST

4.  M3  CRNN + CTC baseline, 3 seeds
5.  M4  PARSeq comparator, identical data and splits
6.  M5  Synthetic rare-glyph ablation (real vs real+synthetic)

7.  M6  DECISION GATE: >= 0.319 on the 500-plate test set?
        yes -> continue    no -> stop and reconsider, do not tune toward a number

8.  M7  Video benchmark; first real measurement of temporal fusion
9.  M8  Integrate behind ANPR_ENGINE, default EasyOCR, dark-shipped
```

Nothing in steps 4–9 is worth starting before step 1 resolves. The model is the
easy part; the lawfully obtained, correctly split, honestly labelled data is the
project.
