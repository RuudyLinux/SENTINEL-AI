"""Run the REAL detection + ANPR pipeline over a video source and report what
it actually detected.

Not a test double: this calls the same `detector.detect_and_track` (YOLOv8 +
ByteTrack), the same `plate_detect.locate_plate`, and the same
`anpr.read_plate`/`passes_anpr_gate` the live camera worker uses. The only
thing it skips is persistence and rule evaluation, so it can be pointed at any
source without touching the database.

Purpose: produce MEASURED detection output from real footage, rather than
asserting the pipeline "works". Every number printed is counted from this run.

Usage:
    python tools/live_detect_probe.py                     # bundled demo footage
    python tools/live_detect_probe.py --source 0          # webcam device 0
    python tools/live_detect_probe.py --source rtsp://... # a real camera
    python tools/live_detect_probe.py --seconds 20 --every 3
"""
from __future__ import annotations

import argparse
import collections
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

DEMO_VIDEO = Path(__file__).resolve().parent.parent / "app" / "demo_assets" / "car-detection.mp4"
VEHICLE_CLASSES = {"car", "truck", "bus", "motorbike"}


def main() -> int:
    import cv2

    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--source", default=str(DEMO_VIDEO), help="video file path, webcam index, or RTSP URL")
    parser.add_argument("--seconds", type=float, default=30.0, help="wall-clock budget")
    parser.add_argument("--every", type=int, default=3, help="run inference every Nth frame (matches detect_every_n_frames)")
    parser.add_argument("--anpr", action="store_true", default=True, help="also run the real ANPR path on vehicles")
    args = parser.parse_args()

    from app.pipeline.anpr import passes_anpr_gate, read_plate
    from app.pipeline import plate_detect
    from app.pipeline.detector import detect_and_track, release_model

    source = int(args.source) if args.source.isdigit() else args.source
    capture = cv2.VideoCapture(source)
    if not capture.isOpened():
        print(f"could not open source: {args.source}", file=sys.stderr)
        return 2

    camera_id = "live-probe"
    frames = inferences = 0
    by_class: collections.Counter = collections.Counter()
    track_ids: set[str] = set()
    ocr_attempts = localized = gate_passed = 0
    plate_reads: list[tuple[str, float]] = []
    inference_seconds = 0.0
    started = time.monotonic()

    try:
        while time.monotonic() - started < args.seconds:
            ok, frame = capture.read()
            if not ok or frame is None:
                break
            frames += 1
            if frames % args.every:
                continue

            t0 = time.monotonic()
            detections = detect_and_track(frame, camera_id, want_person=True, want_vehicle=True)
            inference_seconds += time.monotonic() - t0
            inferences += 1

            for det in detections:
                by_class[det["cls"]] += 1
                if det["track_id"] is not None:
                    track_ids.add(f"{det['cls']}:{det['track_id']}")

                if not args.anpr or det["cls"] not in VEHICLE_CLASSES:
                    continue
                x1, y1, x2, y2 = [max(0, int(v)) for v in det["bbox"]]
                crop = frame[y1:y2, x1:x2]
                if crop.size == 0:
                    continue
                ocr_attempts += 1
                located = plate_detect.locate_plate(crop)
                target = crop
                if located is not None:
                    localized += 1
                    target = located[0]
                _, normalized, confidence = read_plate(target)
                if passes_anpr_gate(normalized, confidence):
                    gate_passed += 1
                    plate_reads.append((normalized, confidence))
    finally:
        capture.release()
        release_model(camera_id)

    elapsed = time.monotonic() - started
    print(f"\nsource                      {args.source}")
    print(f"wall clock                  {elapsed:.1f}s")
    print(f"frames read                 {frames}")
    print(f"inference passes            {inferences} (every {args.every}th frame)")
    print(f"effective inference FPS     {inferences / elapsed:.2f}")
    print(f"mean inference latency      {(inference_seconds / inferences * 1000) if inferences else 0:.0f} ms")
    print(f"\ndetections by class         {dict(by_class) or 'none'}")
    print(f"total detections            {sum(by_class.values())}")
    print(f"unique tracks (ByteTrack)   {len(track_ids)}")
    if args.anpr:
        print(f"\nANPR attempts (vehicles)    {ocr_attempts}")
        print(f"plate region localized      {localized}"
              f"{f' ({localized / ocr_attempts * 100:.0f}%)' if ocr_attempts else ''}")
        print(f"reads passing quality gate  {gate_passed}")
        if plate_reads:
            print("accepted reads:")
            for text, conf in sorted(set(plate_reads), key=lambda r: -r[1])[:15]:
                print(f"    {text:14} confidence {conf:.2f}")
        else:
            print("    (no read cleared looks_like_plate + plate_min_confidence)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
