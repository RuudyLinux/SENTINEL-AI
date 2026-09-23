"""Measure the plate-size distribution of AUTHORIZED deployment footage.

    python analyze_cctv_size.py <footage...> --camera C-014 --out report.md

**Authorization is a precondition, not a formality.** This tool reads whatever
path it is given; it cannot tell an authorized recording from an unauthorized
one. Running it against camera footage without the written data-use agreement
described in `docs/ANPR_M0_DATA_ACQUISITION.md` §2C is not a technical error
this program can catch, and is exactly what M0 blocks.

What it does NOT do, by construction: no network access, no cloud vision API, no
external OCR service, no upload, and no OCR at all. It measures geometry. Plate
TEXT is never read, never stored and never logged — the question here is how
many pixels a plate has, and answering it does not require knowing which vehicle
it is. That keeps this tool usable at a lower privacy tier than annotation.

Design note: OpenCV and the plate detector are imported LAZILY, inside the
functions that need them. The statistics core (`sizing.py`) is stdlib-only and
fully tested without them, so the whole measurement can be verified on synthetic
observations before any footage exists.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from sizing import (
    PlateObservation, SOURCE_DETECTOR, SOURCE_GROUND_TRUTH, UNKNOWN,
    frame_indices, render_report,
)

IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".bmp", ".webp"}
VIDEO_SUFFIXES = {".mp4", ".avi", ".mkv", ".mov", ".m4v", ".ts"}


def _load_detector():
    """Import the repository's plate detector on demand.

    Kept lazy and behind a clear failure message: the training tooling must not
    require the inference stack to be installed, and someone running only the
    synthetic tests should never need torch.
    """
    backend = Path(__file__).resolve().parent.parent / "backend"
    if str(backend) not in sys.path:
        sys.path.insert(0, str(backend))
    try:
        from app.pipeline import plate_detector
        return plate_detector
    except Exception as exc:  # pragma: no cover - depends on the local install
        raise SystemExit(
            f"could not import the plate detector from {backend}: {exc}\n"
            "Install the backend requirements, or supply --annotations to measure "
            "human-drawn boxes instead of detector output."
        ) from exc


def observations_from_annotations(
    path: Path, camera_id: str,
) -> list[PlateObservation]:
    """Read human-drawn boxes from a JSONL manifest.

    The preferred input: these are ground truth, so the resulting statistics are
    not subject to the detector's measured unreliability. Accepts the M1 record
    shape, and also a minimal `{camera_id, frame_index, frame_width,
    frame_height, plate_bbox}` form for a quick manual calibration sample.
    """
    observations: list[PlateObservation] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        payload = json.loads(line)
        bbox = payload.get("plate_bbox")
        if not bbox or len(bbox) != 4:
            continue
        observations.append(PlateObservation(
            camera_id=str(payload.get("camera_id") or camera_id),
            frame_index=int(payload.get("frame_index", 0)),
            frame_width=int(payload.get("image_width") or payload.get("frame_width") or 0),
            frame_height=int(payload.get("image_height") or payload.get("frame_height") or 0),
            bbox=tuple(float(v) for v in bbox),
            source=SOURCE_GROUND_TRUTH,
            track_id=str(payload.get("vehicle_id") or payload.get("track_id") or ""),
            row_layout=str(payload.get("plate_row_layout") or "single"),
            vehicle_category=str(payload.get("vehicle_category") or UNKNOWN),
            plate_face=str(payload.get("plate_face") or UNKNOWN),
            time_of_day=str((payload.get("conditions") or {}).get("time_of_day") or UNKNOWN),
            measured_glyph_px=payload.get("glyph_px"),
            vehicle_bbox=(tuple(float(v) for v in payload["vehicle_bbox"])
                          if payload.get("vehicle_bbox") else None),
        ))
    return observations


def _detect_in_frame(plate_detector, frame, camera_id, frame_index, time_of_day):
    """Every plate region in one frame — all of them, not just the best.

    A frame legitimately contains several vehicles, and measuring only the
    top-scoring box would bias the distribution toward whichever plate happens
    to be largest or most central.
    """
    height, width = frame.shape[:2]
    observations = []
    for box in plate_detector.detect_plates(frame):
        observations.append(PlateObservation(
            camera_id=camera_id,
            frame_index=frame_index,
            frame_width=width,
            frame_height=height,
            bbox=(float(box.x1), float(box.y1), float(box.x2), float(box.y2)),
            source=SOURCE_DETECTOR,
            # No tracker is run here, so each detection is its own "track".
            # Frame-weighted and vehicle-weighted figures will therefore
            # coincide, and the report says so rather than implying a vehicle
            # count it does not have.
            track_id=f"{camera_id}_f{frame_index}_{box.x1}_{box.y1}",
            detector_confidence=float(box.confidence),
            time_of_day=time_of_day,
        ))
    return observations


def analyze_video(
    path: Path, camera_id: str, sample_fps: float, max_frames: int | None,
    max_seconds: float | None, time_of_day: str,
) -> list[PlateObservation]:
    import cv2  # lazy: see the module docstring

    plate_detector = _load_detector()
    capture = cv2.VideoCapture(str(path))
    if not capture.isOpened():
        print(f"could not open {path}", file=sys.stderr)
        return []
    fps = capture.get(cv2.CAP_PROP_FPS) or 25.0
    total = int(capture.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    wanted = frame_indices(total, fps, sample_fps, max_frames, max_seconds)

    observations: list[PlateObservation] = []
    for index in wanted:
        capture.set(cv2.CAP_PROP_POS_FRAMES, index)
        ok, frame = capture.read()
        if not ok:
            continue
        observations.extend(
            _detect_in_frame(plate_detector, frame, camera_id, index, time_of_day)
        )
    capture.release()
    # Deliberately reports counts only — never a filename with a plate in it.
    print(f"  {path.name}: {len(wanted)} frames sampled, {len(observations)} plate regions",
          file=sys.stderr)
    return observations


def analyze_images(
    paths: list[Path], camera_id: str, time_of_day: str,
) -> list[PlateObservation]:
    import cv2  # lazy

    plate_detector = _load_detector()
    observations: list[PlateObservation] = []
    for index, path in enumerate(paths):
        frame = cv2.imread(str(path))
        if frame is None:
            continue
        observations.extend(
            _detect_in_frame(plate_detector, frame, camera_id, index, time_of_day)
        )
    print(f"  {len(paths)} images, {len(observations)} plate regions", file=sys.stderr)
    return observations


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("inputs", nargs="*", type=Path,
                        help="authorized video files, image files, or directories")
    parser.add_argument("--camera", default="unknown", help="opaque camera id (never a URL)")
    parser.add_argument("--time-of-day", default=UNKNOWN, choices=["day", "night", "dusk", UNKNOWN])
    parser.add_argument("--annotations", type=Path,
                        help="JSONL of human-drawn boxes; measures GROUND TRUTH instead of "
                             "detector output, and is strongly preferred")
    parser.add_argument("--sample-fps", type=float, default=2.0,
                        help="frames sampled per second of footage (default 2)")
    parser.add_argument("--max-frames", type=int, default=2000)
    parser.add_argument("--max-seconds", type=float, default=None)
    parser.add_argument("--max-per-track", type=int, default=3,
                        help="vehicle-weighted cap (default 3); 0 disables")
    parser.add_argument("--out", type=Path, help="write the Markdown report here")
    args = parser.parse_args()

    observations: list[PlateObservation] = []
    notes: list[str] = []

    if args.annotations:
        observations.extend(observations_from_annotations(args.annotations, args.camera))
        notes.append(f"ground-truth boxes from {args.annotations.name}")

    files: list[Path] = []
    for item in args.inputs:
        if item.is_dir():
            files.extend(sorted(p for p in item.rglob("*") if p.suffix.lower() in
                                IMAGE_SUFFIXES | VIDEO_SUFFIXES))
        elif item.exists():
            files.append(item)

    videos = [p for p in files if p.suffix.lower() in VIDEO_SUFFIXES]
    images = [p for p in files if p.suffix.lower() in IMAGE_SUFFIXES]
    for video in videos:
        observations.extend(analyze_video(
            video, args.camera, args.sample_fps, args.max_frames,
            args.max_seconds, args.time_of_day,
        ))
    if images:
        observations.extend(analyze_images(images, args.camera, args.time_of_day))
    if videos or images:
        notes.append(
            f"interval sampling at {args.sample_fps} fps, max {args.max_frames} frames "
            f"per video, over {len(videos)} video(s) and {len(images)} image(s)"
        )

    if not observations:
        print("No observations produced. Supply authorized footage or an annotations file.\n"
              "No statistics are reported, because none were measured.", file=sys.stderr)
        return 1

    report = render_report(
        observations, max_per_track=args.max_per_track, sampling_note="; ".join(notes),
    )
    if args.out:
        args.out.write_text(report, encoding="utf-8")
        print(f"wrote {args.out}", file=sys.stderr)
    else:
        print(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
