"""ANPR benchmark on real video sequences, per tracked vehicle.

    python tools/anpr_seq_bench.py run  SEQ_DIR OUT_DIR --plate-model M.pt
    python tools/anpr_seq_bench.py score OUT_DIR/result.json truth.json

`run` replays frames (SEQ_DIR/*.jpg, in order) through the live pipeline's own
plate path (worker._read_plate_for_track, then plate_tracker voting) for every
ByteTrack vehicle track, at a fixed AI rate. It writes each track's best plate
crop for review and what the system would have PUBLISHED for that track
(corroborated + gate-passing), plus its best single read.

truth.json maps track id -> the plate a reviewer reads in the crops, or
"UNREADABLE" when the plate cannot be resolved at source resolution. Unreadable
plates are reported separately and are not counted as OCR failures.

Footage is not committed (see tools/anpr_corpus/README.md).
"""
import argparse
import asyncio
import difflib
import json
import os
import sys
import time
from pathlib import Path

import cv2

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def cmd_run(args):
    os.environ.setdefault("DB_PATH", str(Path(args.out) / "unused.db"))
    from app.config import settings
    settings.plate_model_name = args.plate_model or ""
    from ultralytics import YOLO
    from app.pipeline import anpr, plate_detector, plate_tracker, worker
    plate_detector.get_plate_model.cache_clear()

    out = Path(args.out)
    (out / "crops").mkdir(parents=True, exist_ok=True)
    frames = sorted(Path(args.seq).glob("*.jpg"))[:: args.step]
    model = YOLO(args.detector)
    cam = f"bench-{time.time_ns()}"
    tracks: dict[str, dict] = {}
    ocr_ms = []

    async def go():
        for frame_path in frames:
            img = cv2.imread(str(frame_path))
            r = model.track(img, classes=[2, 3, 5, 7], conf=0.4, persist=True, tracker="bytetrack.yaml", verbose=False)[0]
            if r.boxes is None or r.boxes.id is None:
                continue
            for box, tid in zip(r.boxes.xyxy.tolist(), r.boxes.id.int().tolist()):
                x1, y1, x2, y2 = (max(0, int(v)) for v in box)
                crop = img[y1:y2, x1:x2]
                if crop.size == 0:
                    continue
                key = str(tid)
                t = tracks.setdefault(key, {"reads": [], "best_h": 0})
                state = plate_tracker.touch(cam, key)
                if plate_tracker.should_ocr(cam, key):
                    started = time.perf_counter()
                    read, pbox, pcrop = await worker._read_plate_for_track(crop, "bench", x1, y1)
                    ocr_ms.append((time.perf_counter() - started) * 1000)
                    if anpr.passes_read_gate(read):
                        state = plate_tracker.record_read(cam, key, read.normalized, read.confidence, read.raw, pbox,
                                                          variant=read.variant, variants_agreeing=read.variants_agreeing,
                                                          plate_crop=pcrop)
                    else:
                        plate_tracker.mark_ocr_attempt(cam, key)
                    t["reads"].append({"text": read.normalized, "conf": round(read.confidence, 3),
                                       "gate": bool(anpr.passes_read_gate(read))})
                    if pcrop is not None and pcrop.shape[0] * pcrop.shape[1] > t["best_h"]:
                        t["best_h"] = pcrop.shape[0] * pcrop.shape[1]
                        cv2.imwrite(str(out / "crops" / f"T{key}.jpg"), pcrop)
                best = state.best()
                t["published"] = best[0] if best and plate_tracker.has_consensus(cam, key) else None
                t["best_read"] = best[0] if best else None
    asyncio.run(go())
    result = {"plate_model": args.plate_model, "frames": len(frames), "step": args.step,
              "ocr_ms_median": sorted(ocr_ms)[len(ocr_ms) // 2] if ocr_ms else 0, "ocr_calls": len(ocr_ms),
              "tracks": tracks}
    (out / "result.json").write_text(json.dumps(result, indent=1))
    print(f"{len(tracks)} tracks, {len(ocr_ms)} OCR calls, median {result['ocr_ms_median']:.0f} ms")


def char_accuracy(pred: str, truth: str) -> float:
    return difflib.SequenceMatcher(None, pred or "", truth).ratio()


def cmd_score(args):
    result = json.loads(Path(args.result).read_text())
    truth = json.loads(Path(args.truth).read_text())
    rows, readable, unreadable = [], 0, 0
    published_ok = published_wrong = rejected = 0
    best_ok, char_scores = 0, []
    for tid, gt in truth.items():
        t = result["tracks"].get(tid, {})
        if gt == "UNREADABLE":
            unreadable += 1
            if t.get("published"):
                published_wrong += 1  # anything published for an unreadable plate is a false read
            continue
        readable += 1
        best = t.get("best_read") or ""
        pub = t.get("published")
        char_scores.append(char_accuracy(best, gt))
        best_ok += best == gt
        if pub is None:
            rejected += 1
        elif pub == gt:
            published_ok += 1
        else:
            published_wrong += 1
        rows.append((gt, best, pub, round(char_accuracy(best, gt), 2)))
    for gt, best, pub, ca in rows:
        print(f"  {gt:12} best read {best or '-':12} published {pub or '-':12} char acc {ca}")
    published = published_ok + published_wrong
    print(f"readable plates {readable}, unreadable {unreadable}")
    print(f"best-read full match {best_ok}/{readable}; mean char accuracy "
          f"{(sum(char_scores) / len(char_scores)) if char_scores else 0:.2f}")
    print(f"published: correct {published_ok}, WRONG {published_wrong}, rejected (no verified plate) {rejected}")
    print(f"false-read rate among published: {published_wrong / published if published else 0:.2f}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("seq")
    r.add_argument("out")
    r.add_argument("--detector", default="yolov8s.pt")
    r.add_argument("--plate-model", default="")
    r.add_argument("--step", type=int, default=2)
    s = sub.add_parser("score")
    s.add_argument("result")
    s.add_argument("truth")
    args = ap.parse_args()
    {"run": cmd_run, "score": cmd_score}[args.cmd](args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
