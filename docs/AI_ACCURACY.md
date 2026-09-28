# AI accuracy — measured on real grid footage (2026-09-28)

Every number here was measured on footage from the government camera grid. No
synthetic detections, no Trigger Scenario data. The footage is not committed.
The harnesses that produced the numbers are, so any result can be re-run:
`backend/tools/ai_bench.py` (detection) and `backend/tools/anpr_seq_bench.py`
(ANPR).

## Detection

### Benchmark set

| | |
|---|---|
| Images | 24 stills, 1920×1080 (cam12: 1280×720), 22 grid cameras |
| Lighting | 6 daylight, 18 night (the grid was mostly showing night footage when captured) |
| Excluded | 2 frames that the grid delivered as grey decoder garbage; 9 cameras with a corrupted, black or empty scene |
| Scored objects | ≥60px tall at 1080p (near/mid field, where zone rules and ANPR operate) |
| Reference | 141 vehicles + 107 persons from strong-model agreement, plus 40 vehicles + 26 persons that no model found (counted by eye) |

**How the reference was built, and what that means.** A box enters the reference
when at least 2 of 3 strong runs (yolov8x@1280, yolov8x@960, yolo11m@1280)
detect it. Every image was then reviewed by eye, and objects that no model boxed
were counted as `missed`. They are false negatives for every configuration, so
recall is measured against everything visible, not only what some model found.
The reviewer was an AI assistant, not a trained annotator. yolo11m helped build
the reference, so its own scores are biased upward and it was not eligible for
selection.

### Results (vehicles and persons scored separately; P = precision, R = recall)

| Model | Size | Conf | IoU | Vehicle P | Vehicle R | Vehicle F1 | Person P | Person R | Person F1 | CPU ms |
|---|---|---|---|---|---|---|---|---|---|---|
| yolov8n | 640 | 0.40 | 0.7 | 0.833 | 0.193 | 0.314 | 0.950 | 0.144 | 0.250 | — |
| yolov8s (previous default) | 640 | 0.40 | 0.7 | 0.842 | 0.354 | 0.498 | 1.000 | 0.288 | 0.447 | 74 |
| yolov8s | 640 | 0.30 | 0.5 | 0.713 | 0.425 | 0.533 | 0.895 | 0.386 | 0.540 | 74 |
| yolov8s | 1280 | 0.25 | 0.5 | 0.761 | 0.564 | 0.648 | 0.889 | 0.606 | 0.721 | — |
| yolo11n | 1280 | 0.25 | 0.5 | 0.758 | 0.414 | 0.536 | 0.905 | 0.432 | 0.585 | — |
| yolo11s | 640 | 0.30 | 0.5 | 0.765 | 0.431 | 0.551 | 0.929 | 0.394 | 0.553 | — |
| **yolo11s (selected)** | **960** | **0.30** | **0.5** | **0.860** | **0.508** | **0.639** | **0.959** | **0.538** | **0.689** | **123** |
| yolo11s | 960 | 0.25 | 0.5 | 0.800 | 0.552 | 0.654 | 0.923 | 0.545 | 0.686 | 123 |
| yolov8m | 960 | 0.40 | 0.5 | 0.858 | 0.503 | 0.634 | 0.959 | 0.530 | 0.683 | 349 |
| yolov8m | 960 | 0.30 | 0.5 | 0.793 | 0.613 | 0.692 | 0.860 | 0.606 | 0.711 | 349 |
| yolov8m | 1280 | 0.25 | 0.5 | 0.743 | 0.591 | 0.658 | 0.798 | 0.689 | 0.740 | — |

The full 72-configuration matrix (6 models × 640/960/1280 × conf 0.25/0.40 × IoU
0.5/0.7) is reproducible with `ai_bench.py evaluate`.

**Why yolo11s @ 960, conf 0.30, IoU 0.5:**
- Precision is higher than the previous default (0.860 vs 0.842), so it does not
  buy recall with false alarms.
- Vehicle recall rises from 0.354 to 0.508, and person recall from 0.288 to 0.538.
- yolov8m gives the same accuracy at 3× the CPU time.
- Bigger is not always better: 1280 was worse than 960 for several models.

Night vs day, for the selected configuration: day vehicle F1 ≈ 0.81, night ≈ 0.59.
Night is limited by headlight glare and by the grid's own compression damage.
Even the strong reference models miss large near objects in some night frames.

### Night preprocessing (18 night images, yolo11s @ 960)

| Preprocessing | Vehicle F1 | Person F1 | Cost |
|---|---|---|---|
| **none** | **0.586** | **0.681** | — |
| gamma 0.7 | 0.568 | 0.670 | +4 ms |
| denoise (NLM) | 0.566 | 0.620 | +5.9 s |
| CLAHE | 0.537 | 0.630 | +33 ms |
| CLAHE + gamma | 0.513 | 0.626 | +33 ms |

Every variant was worse, and CLAHE also lowered precision (0.833 → 0.735). No
preprocessing is applied.

## Tracking (ByteTrack)

There is no identity ground truth, so these are proxies: **fragments** are track
IDs that exist for ≤2 processed frames, and **churn** is distinct IDs per
object-second (lower is steadier). ID switches could not be measured.

Sequences: day cam06 (20s, 473 frames) and night cam02 (20s, 521 frames), replayed
at the live AI rate.

| Config @ 2.5 AI fps | Day fragments | Night fragments | Day churn | Night churn | Night median life |
|---|---|---|---|---|---|
| previous (v8s 640, conf 0.40, default tracker) | 30% | 40% | 0.656 | 0.332 | 1.5s |
| v11s 960, conf 0.30, default tracker | 33% | 69% | 0.546 | 0.535 | 0.0s |
| + feed 0.10 / publish 0.30 | 33% | 66% | 0.546 | 0.550 | 0.0s |
| + new_track 0.40 | 36% | 60% | 0.560 | 0.368 | 0.0s |
| + match 0.9 | 0% | 44% | 0.332 | 0.278 | 2.7s |
| **+ track_buffer 60 (selected)** | **0%** | **35%** | **0.332** | **0.256** | **6.5s** |

At 5 AI fps the selected config gives 9% day / 33% night fragments, against
25% / 44% for the previous one.

The biggest single change is `match_thresh 0.9`. At 2.5 frames/s an object moves
far between frames, and the default 0.8 split one vehicle into several IDs. Night
fragmentation stays high: glare makes detections appear and vanish, and no
tracker setting fixes a missing detection. Settings:
`backend/app/pipeline/bytetrack_sentinel.yaml`.

### ID merges (checked 2026-09-28)

A more permissive match can give two objects one ID. Without identity ground
truth, a suspected merge is an ID whose box changes kind, jumps more than one box
diagonal per frame, or changes area more than 3× between observations. Rider and
motorbike trading an ID is counted separately: it is one moving object.

| Suspected merges per 20s sequence | match 0.8 | match 0.9 (selected) |
|---|---|---|
| day cam06, 2.5 fps | 1 | 2 |
| night cam02, 2.5 fps | 2 | 6 |
| day cam06, 8 fps (GPU rate) | 6 | 8 |
| night cam02, 8 fps | 1 | 5 |

The night cases were checked by eye, and most are real merges: a car's ID taken
over seconds later by a motorbike rider, a rider's ID by a car. ByteTrack
associates by position only. An ID whose object changes kind (car/bus/truck
against person/motorbike) is now given a new ID (`detector._published_track_id`).
Replayed through the app, cross-kind merges fell to 0 on both sequences, and night
fragmentation at 2.5 fps rose from 35% to 45%, because each broken-up merge
leaves a short piece. In a 10-minute live run on cam02, 66 of 537 IDs (12%) were
split this way. A merge between two objects of the same kind (car into car) is not
detected.

## ANPR

### Benchmark

cam06 daytime sequence, 20s: 21 vehicle tracks, replayed through the app's own
plate path (localizer → crop → EasyOCR → format repair → per-track voting).
Ground truth was read by eye from the plate crops. Only **3 plates are readable
at source resolution** (GJ11EB9402, GJ11CM4644, GJ03FK8807). The other 18 are
two-wheeler plates about 65px wide and blurred, and are **source not readable**:
they are not counted as OCR failures. All readable plates are Gujarat (GJ). The
grid offered no other state's plates, so MH/RJ/MP are **not testable**.

| Config | Plate located | Full-plate correct | Mean char accuracy (readable) | Wrong plates published | OCR ms (median call) |
|---|---|---|---|---|---|
| A: classical localizer + EasyOCR (previous) | 2/17 | 0/3 | 0.00 | 0 | 119 |
| **B: plate detector + EasyOCR (selected)** | **9/17** | **0/3** | **0.58** (0.84, 0.90, 0.00) | **0** | 192 |
| C: plate detector + fast-plate-ocr (cct-xs-v2) | 9/17 | 0/3 | 0.55 | **2 (100% of published)** | 61 |

**Config C was rejected and removed.** The plate-specific OCR is 60× faster and
reads individual characters well, but its mistakes are consistent from frame to
frame, and it reports ~0.99 per-character confidence on wrong characters.
Temporal voting then agrees with the mistake, and the format check passes it
because `GJ11E8940` and `GJ03EX8807` are valid Indian plates. It published two
wrong plates. For a police system, that is the failure to avoid.

**Config B was adopted.** The plate detector is morsetechlab
yolov11-license-plate-detection v1n, AGPL-3.0. It finds the plate on 9 of 17
vehicles against 2 for the classical localizer. It raised character accuracy
from 0 to 0.58 and published no wrong plate. It still read **no plate fully
correctly**. The best reads were `GJ03FK8207` (one digit wrong) and `GJ1EO9402`.

**Why ANPR accuracy cannot be claimed.** At this grid's resolution, the readable
plates are 66-155px wide. That is at the edge of what any general OCR engine
resolves, and the one plate-specific model tested is confidently wrong. A
plate-specific OCR model trained on Indian plates and evaluated against this
benchmark is what would move the number. The source cameras also limit it: 18
of 21 plates are not readable even by eye.

**Live check (2026-09-28).** In about 5 minutes of GPU processing on cam06 (day
traffic), one plate read was stored: `GJ0RS911`, confidence 0.36, seen once,
from the whole vehicle crop. It is marked *pending review*, and nothing was
auto-accepted. It is not a valid plate format and is probably wrong. This is the
intended behaviour: an uncertain read goes to an operator and never becomes a
settled identity. On night cam02 no plate was read in 12 minutes: **ANPR not
validated on that camera source.**

**Second sequence check (2026-09-28, later).** Two fresh real sequences were
replayed through the same plate path (`tools/anpr_seq_bench.py run`, selected
config B, CUDA). Ground truth read by eye from the source pixels.

| Sequence | Tracks | Plates readable by eye | Published | Correct | Wrong published |
|---|---|---|---|---|---|
| GRID-cam02, night, 614 frames at 8 fps | 99 | 0 (glare and motion blur; **source not readable**) | 0 | — | 0 |
| GRID-cam06, day, 261 frames at 8 fps | 24 | 3: `GJ32AG2883` (green EV plate, ~90×26 px), `GJ11CJ7578` (~97×24 px), two-line `GJ11C/K1044` (~64×46 px) | 1 | 1: `GJ32AG2883`, 10/10 characters, confidence 0.51 | 0 |
| GRID-cam15, night RLVD, 65 frames | 8 | 0 | 0 | — | 0 |

On cam06 the gate passed five reads of the green plate, two of them wrong
(`GJ32IG2887`, `GJ32A6288`); temporal voting published the correct one. The two
other readable plates are about 24 px tall and were not read at all, so
nothing wrong was published for them. Complete-plate accuracy on readable
plates in this check: **1/3**. False plates published: **0**. This is three
plates; it shows the path can produce a correct plate and holds back wrong
ones, not a rate.

Rejected in the same check: `PLATE_PREPROCESS_VARIANTS=original,sharpen,adaptive`
on the cam06 sequence published nothing (the correct plate was lost to the
variant-agreement rule) at 2.2× the OCR time.

**Detection benchmark not re-run.** The 24 labelled frames above lived outside
git in a session scratch directory that no longer exists, so the detection
figures in this document were not re-measured on 2026-09-28. No detection
setting changed that day.

**Watchlist → alert on a real plate: NOT VALIDATED.** A live 12-minute GPU run
on GRID-cam06 (2,877 frames inferred) with an operator watchlist entry for
`GJ32AG2883` — a plate seen in that camera's own recorded footage — produced
one plate read, `GJ32A4286` (confidence 0.54-0.56, one read, almost certainly
the same green plate misread). It was held as *pending review*, was not
auto-accepted, and did not match the watchlist. No match fired and none was
injected. The path from a correct live read to an alert is covered by the test
suite but has not been shown on a live grid camera.

## Performance (live app, real grid cameras, CPU, DETECT_EVERY_N_FRAMES=1)

| Test | System CPU | RAM | AI fps / camera | Inference | Frame age | API |
|---|---|---|---|---|---|---|
| previous config, 1 camera | 14-57% | 1.0 GB | 2.4 | 57 ms | <100 ms | 15 ms |
| previous config, 2 cameras | 93% | 1.3 GB | 2.4-2.8 | 114 ms | <200 ms | 8 ms |
| **selected config, 1 camera** | **92%** | **1.15 GB** | **2.0** | **126 ms** | **219 ms** | **7 ms** |
| selected config, 2 cameras | 100% | 1.47 GB | 1.1-1.5 | 220 ms | 16-32 ms | 8-11 ms |

The accuracy gain costs CPU: on this machine the selected configuration runs
**one AI camera**. For two, set `DETECTOR_IMGSZ=640`: vehicle F1 0.551, still
above the previous 0.498.

**GPU (measured live, optional runtime).** The machine has an RTX 3050 Ti Laptop
GPU (4 GB). The default environment stays CPU-only. A separate CUDA environment
(README, "GPU runtime") runs the same code, and the benchmark detections are
identical on CPU and GPU. Live on real grid cameras:

| Test | App CPU | AI fps / camera | Inference | GPU util | GPU memory (process) | Frame age |
|---|---|---|---|---|---|---|
| GPU, 1 camera | 22% | 8.2 | 16 ms | 25-48% | ~1.2 GB | 78 ms |
| GPU, 2 cameras | 32% | 6.2 / 6.9 | 26 / 38 ms | 27-64% | ~2.7 GB | <100 ms |
| GPU, 1 camera, 10.5 min, another heavy process on the CPU | 9% (app only) | 4.4 | 32-49 ms | 0-52% | ~1.2 GB | median 0 ms, max 953 ms during one grid stall |

The 10.5-minute run had 0 crashes and 0 database lock errors. The thread count
was steady (61-66), and there were 4 reconnects after a grid stall, all
automatic. With the GPU runtime, `MAX_AI_CAMERAS=2` is the tested setting. A
third camera is not recommended on a 4 GB card.
