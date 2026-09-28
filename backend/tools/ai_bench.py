"""Reproducible detection benchmark on labelled real footage.

    python tools/ai_bench.py candidates DATASET_DIR --models yolov8x.pt yolo11m.pt
    python tools/ai_bench.py evaluate   DATASET_DIR --models yolov8s.pt --sizes 640 960 1280

DATASET_DIR has images/*.jpg and labels.json. Grid footage isn't committed
(same as tools/anpr_corpus); the dataset lives outside git and this tool
makes results on it reproducible.

labels.json:

    {"GRID-cam04_0001.jpg": {
        "lighting": "night",                       # "day" | "night"
        "objects": [{"cls": "car", "bbox": [x1, y1, x2, y2]}, ...],
        "missed": {"vehicle": 2, "person": 1}}}    # visible, but boxed by no candidate model

Building the ground truth:

1. `candidates` runs several big models at low confidence and merges their
   boxes into a numbered pool per image for review.
2. A reviewer keeps real objects (fixing the class), drops the rest, and
   counts objects NO model boxed as `missed`. Those can't match, so they're
   false negatives for every config: recall is against everything visible.
3. `evaluate` scores configs against that. Matching ignores class within a
   group: car/bus/truck/motorbike are all "vehicle" (an autorickshaw called
   car by one model and truck by another is right either way for every rule
   here). "person" is its own group.

Only objects at least MIN_HEIGHT_FRAC of the frame tall (60px at 1080p) are
scored, both sides: that's where zone rules and ANPR work, and smaller boxes
can't be reviewed reliably. Predictions under the cutoff are ignored, not
counted as false positives.
"""
import argparse
import json
import statistics
import time
from pathlib import Path

import cv2
import numpy as np

VEHICLE = {"car", "truck", "bus", "motorbike", "motorcycle"}
COCO_IDS = {0: "person", 2: "car", 3: "motorbike", 5: "bus", 7: "truck"}
MIN_HEIGHT_FRAC = 60 / 1080
MATCH_IOU = 0.5


def group(cls: str) -> str:
    return "vehicle" if cls in VEHICLE else "person"


def iou(a, b) -> float:
    ix = max(0.0, min(a[2], b[2]) - max(a[0], b[0]))
    iy = max(0.0, min(a[3], b[3]) - max(a[1], b[1]))
    inter = ix * iy
    union = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / union if union > 0 else 0.0


def big_enough(bbox, frame_h: int) -> bool:
    return (bbox[3] - bbox[1]) >= MIN_HEIGHT_FRAC * frame_h


def predict(model, img, size: int, conf: float, nms_iou: float, preprocess=None):
    if preprocess is not None:
        img = preprocess(img)
    r = model.predict(img, imgsz=size, conf=conf, iou=nms_iou, classes=list(COCO_IDS), verbose=False)[0]
    return [
        {"cls": COCO_IDS[int(c)], "conf": float(p), "bbox": [float(v) for v in b]}
        for b, c, p in zip(r.boxes.xyxy.tolist(), r.boxes.cls.tolist(), r.boxes.conf.tolist())
    ]


def score_image(preds, label, frame_h: int):
    """Greedy highest-confidence-first matching within each group."""
    out = {}
    for g in ("vehicle", "person"):
        gts = [o["bbox"] for o in label["objects"] if group(o["cls"]) == g and big_enough(o["bbox"], frame_h)]
        ps = sorted((p for p in preds if group(p["cls"]) == g and big_enough(p["bbox"], frame_h)),
                    key=lambda p: -p["conf"])
        used = set()
        tp = 0
        for p in ps:
            best, best_i = 0.0, None
            for i, gt in enumerate(gts):
                if i not in used:
                    v = iou(p["bbox"], gt)
                    if v > best:
                        best, best_i = v, i
            if best >= MATCH_IOU:
                used.add(best_i)
                tp += 1
        fp = len(ps) - tp
        fn = len(gts) - tp + int(label.get("missed", {}).get(g, 0))
        out[g] = (tp, fp, fn)
    return out


def prf(tp: int, fp: int, fn: int):
    p = tp / (tp + fp) if tp + fp else 0.0
    r = tp / (tp + fn) if tp + fn else 0.0
    f = 2 * p * r / (p + r) if p + r else 0.0
    return p, r, f


# --- preprocessing variants tested on night frames ------------------------

def clahe(img):
    lab = cv2.cvtColor(img, cv2.COLOR_BGR2LAB)
    lab[:, :, 0] = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8)).apply(lab[:, :, 0])
    return cv2.cvtColor(lab, cv2.COLOR_LAB2BGR)


def gamma(img, g=0.7):
    table = np.array([((i / 255.0) ** g) * 255 for i in range(256)], dtype=np.uint8)
    return cv2.LUT(img, table)


def denoise(img):
    return cv2.fastNlMeansDenoisingColored(img, None, 5, 5, 7, 21)


PREPROCESS = {"none": None, "clahe": clahe, "gamma0.7": gamma, "denoise": denoise,
              "clahe+gamma": lambda i: clahe(gamma(i))}


def cmd_candidates(args):
    from ultralytics import YOLO
    ds = Path(args.dataset)
    out = ds / "review"
    out.mkdir(exist_ok=True)
    models = [YOLO(m) for m in args.models]
    pool = {}
    for img_path in sorted((ds / "images").glob("*.jpg")):
        img = cv2.imread(str(img_path))
        boxes = []
        for m in models:
            for size in (640, 1280):
                boxes += predict(m, img, size, 0.15, 0.6)
        boxes.sort(key=lambda b: -b["conf"])
        merged = []
        for b in boxes:  # cross-model NMS within a group
            if all(not (group(b["cls"]) == group(k["cls"]) and iou(b["bbox"], k["bbox"]) > 0.5) for k in merged):
                merged.append(b)
        merged = [b for b in merged if big_enough(b["bbox"], img.shape[0])]
        pool[img_path.name] = merged
        vis = img.copy()
        for i, b in enumerate(merged):
            x1, y1, x2, y2 = (int(v) for v in b["bbox"])
            colour = (0, 255, 255) if group(b["cls"]) == "vehicle" else (255, 0, 255)
            cv2.rectangle(vis, (x1, y1), (x2, y2), colour, 2)
            cv2.putText(vis, f"{i}{b['cls'][0]}", (x1 + 2, y1 + 22), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 0, 255), 2)
        cv2.imwrite(str(out / img_path.name), vis, [cv2.IMWRITE_JPEG_QUALITY, 85])
    (ds / "candidates.json").write_text(json.dumps(pool, indent=1))
    print(f"{len(pool)} images, {sum(len(v) for v in pool.values())} candidate boxes -> {out}")


def cmd_reference(args):
    """Reference labels by agreement: a box goes in when at least `--votes`
    strong runs detect it (IoU > 0.5, same group). Much better than the
    deployable models but still not ground truth; `audit` renders it so a
    reviewer can count label errors and misses, and the report has to say
    it's model agreement, audited."""
    from ultralytics import YOLO
    ds = Path(args.dataset)
    runs = []
    for spec in args.runs:
        name, size = spec.rsplit("@", 1)
        runs.append((YOLO(name), int(size)))
    lighting = json.loads(Path(args.lighting_json).read_text())
    labels = {}
    for img_path in sorted((ds / "images").glob("*.jpg")):
        if img_path.name not in lighting:
            continue
        img = cv2.imread(str(img_path))
        per_run = [predict(m, img, size, args.conf, 0.6) for m, size in runs]
        clusters = []
        for run_idx, boxes in enumerate(per_run):
            for b in boxes:
                for c in clusters:
                    if group(c["best"]["cls"]) == group(b["cls"]) and iou(c["best"]["bbox"], b["bbox"]) > 0.5:
                        c["runs"].add(run_idx)
                        if b["conf"] > c["best"]["conf"]:
                            c["best"] = b
                        break
                else:
                    clusters.append({"best": b, "runs": {run_idx}})
        objects = [{"cls": c["best"]["cls"], "bbox": c["best"]["bbox"]}
                   for c in clusters if len(c["runs"]) >= args.votes and big_enough(c["best"]["bbox"], img.shape[0])]
        labels[img_path.name] = {"lighting": lighting[img_path.name], "objects": objects, "missed": {}}
        vis = img.copy()
        for i, o in enumerate(objects):
            x1, y1, x2, y2 = (int(v) for v in o["bbox"])
            colour = (0, 255, 255) if group(o["cls"]) == "vehicle" else (255, 0, 255)
            cv2.rectangle(vis, (x1, y1), (x2, y2), colour, 3)
        (ds / "audit").mkdir(exist_ok=True)
        cv2.imwrite(str(ds / "audit" / img_path.name), vis, [cv2.IMWRITE_JPEG_QUALITY, 85])
    (ds / "labels.json").write_text(json.dumps(labels, indent=1))
    print(f"{len(labels)} images, {sum(len(v['objects']) for v in labels.values())} reference objects")


def cmd_evaluate(args):
    from ultralytics import YOLO
    ds = Path(args.dataset)
    labels = json.loads((ds / "labels.json").read_text())
    images = {name: cv2.imread(str(ds / "images" / name)) for name in labels}
    rows = []
    for model_name in args.models:
        model = YOLO(model_name)
        for size in args.sizes:
            for nms_iou in args.ious:
                for pre in args.preprocess:
                    fn = PREPROCESS[pre]
                    model.predict(next(iter(images.values())), imgsz=size, verbose=False)  # warm-up
                    raw, times = {}, []
                    for name, img in images.items():
                        if args.lighting and labels[name].get("lighting") != args.lighting:
                            continue
                        t = time.perf_counter()
                        raw[name] = predict(model, img, size, min(args.confs), nms_iou, fn)
                        times.append((time.perf_counter() - t) * 1000)
                    for conf in args.confs:
                        tot = {g: [0, 0, 0] for g in ("vehicle", "person")}
                        split = {}
                        for name, preds in raw.items():
                            kept = [p for p in preds if p["conf"] >= conf]
                            s = score_image(kept, labels[name], images[name].shape[0])
                            light = labels[name].get("lighting", "?")
                            for g, v in s.items():
                                for k in range(3):
                                    tot[g][k] += v[k]
                                    split.setdefault((light, g), [0, 0, 0])[k] += v[k]
                        row = {"model": Path(model_name).stem, "size": size, "conf": conf, "iou": nms_iou,
                               "preprocess": pre, "latency_ms": round(statistics.median(times), 1),
                               "images": len(raw)}
                        for g in ("vehicle", "person"):
                            p, r, f = prf(*tot[g])
                            row.update({f"{g}_P": p, f"{g}_R": r, f"{g}_F1": f, f"{g}_counts": tot[g]})
                        for (light, g), v in split.items():
                            row[f"{light}_{g}_F1"] = prf(*v)[2]
                            row[f"{light}_{g}_R"] = prf(*v)[1]
                            row[f"{light}_{g}_P"] = prf(*v)[0]
                        rows.append(row)
                        print(f"{row['model']:9} {size:5} conf {conf:.2f} iou {nms_iou:.2f} {pre:11} | "
                              f"veh P {row['vehicle_P']:.3f} R {row['vehicle_R']:.3f} F1 {row['vehicle_F1']:.3f} | "
                              f"per P {row['person_P']:.3f} R {row['person_R']:.3f} F1 {row['person_F1']:.3f} | "
                              f"{row['latency_ms']} ms", flush=True)
    if args.json:
        Path(args.json).write_text(json.dumps(rows, indent=1))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("candidates")
    c.add_argument("dataset")
    c.add_argument("--models", nargs="+", required=True)
    rf = sub.add_parser("reference")
    rf.add_argument("dataset")
    rf.add_argument("--runs", nargs="+", required=True, help="model@size, e.g. yolov8x.pt@1280")
    rf.add_argument("--votes", type=int, default=2)
    rf.add_argument("--conf", type=float, default=0.25)
    rf.add_argument("--lighting-json", required=True)
    e = sub.add_parser("evaluate")
    e.add_argument("dataset")
    e.add_argument("--models", nargs="+", required=True)
    e.add_argument("--sizes", nargs="+", type=int, default=[640])
    e.add_argument("--confs", nargs="+", type=float, default=[0.25, 0.4])
    e.add_argument("--ious", nargs="+", type=float, default=[0.7])
    e.add_argument("--preprocess", nargs="+", default=["none"], choices=list(PREPROCESS))
    e.add_argument("--lighting", choices=["day", "night"])
    e.add_argument("--json")
    args = ap.parse_args()
    {"candidates": cmd_candidates, "reference": cmd_reference, "evaluate": cmd_evaluate}[args.cmd](args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
