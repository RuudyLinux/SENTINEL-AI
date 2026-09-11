"""Camera capacity benchmark (10/10 roadmap P4) — measure before claiming.

Answers "how many cameras can one deployment safely process?" the same way
`tools/anpr_bench.py` answers ANPR accuracy: by actually running the real
pipeline and reporting real numbers, never by inspecting a config value.
`sentinel_grid_max_autoconnect` (see config.py) is explicitly a conservative
GUESS pending this measurement — this tool is what turns it into one.

What it does
------------
For each camera COUNT in `--stages` (default "1,3,5"), it:

1. Registers that many `video_file` cameras against the repo's bundled demo
   video (`app/demo_assets/car-detection.mp4`), each with AI person/vehicle/
   ANPR enabled — i.e. the FULL real pipeline (YOLOv8 detection, ByteTrack,
   plate localization, EasyOCR), not a stub.
2. Starts their real worker tasks (`pipeline.worker.start_worker`), staggered
   the same way `sentinel_grid_stagger_seconds` staggers real grid cameras.
3. Samples `worker.CAMERA_STATS` (per-camera FPS/inference-ms/read-ms EMAs,
   already computed by the live pipeline for its own diagnostics) and
   process-wide CPU/RSS (psutil) once per second for `--duration` seconds.
4. Stops every worker, tears down that stage's camera rows, and moves to the
   next stage — so stage N's load never carries into stage N+1's numbers.

What it reports
----------------
Per stage: mean/p95 FPS, inference latency, frame-read latency, process
CPU%, process RSS, and how many cameras never produced a single stats
sample (a real, honest "this one didn't come up" signal, not silently
dropped from the average). Written as JSON (machine-readable) and Markdown
(the operating-envelope statement the roadmap asks for) — see `--json`/`--md`.

Honesty notes
-------------
- This measures ONE machine, right now, on the bundled 720p demo clip decoded
  N times over — not N different real 1080p RTSP streams, not a different
  host, not 80,000 cameras. The report says so; do not strip that caveat out
  when relaying the numbers.
- If a stage cannot even start (e.g. not enough decode bandwidth), that is
  itself the answer this tool exists to find — it is reported as a failed
  stage, not silently skipped.
- Uses the SAME database file `app.config.settings` already points at unless
  `DB_PATH` is overridden — set `DB_PATH` to a scratch file before running
  this against a machine with real operational data, so benchmark camera
  rows never land in a real deployment's database.

Usage
-----
    cd backend
    .venv/Scripts/python.exe tools/camera_bench.py --stages 1,3,5 --duration 20 \\
        --json camera_bench_results.json --md camera_bench_report.md
"""
from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

DEMO_VIDEO = Path(__file__).resolve().parent.parent / "app" / "demo_assets" / "car-detection.mp4"


def _percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    idx = min(len(ordered) - 1, int(round(pct / 100 * (len(ordered) - 1))))
    return ordered[idx]


async def _run_stage(db_session_factory, count: int, duration: float, stagger: float) -> dict:
    from app import models
    from app.pipeline.worker import CAMERA_STATS, start_worker, stop_worker

    db = db_session_factory()
    camera_ids: list[str] = []
    try:
        for i in range(count):
            cam = models.Camera(
                camera_code=f"BENCH-{uuid.uuid4().hex[:8]}", name=f"Bench camera {i + 1}",
                source_type="video_file", source_uri=str(DEMO_VIDEO),
                ai_person=True, ai_vehicle=True, ai_anpr=True,
            )
            db.add(cam)
            db.flush()
            camera_ids.append(cam.id)
        db.commit()

        started_at = time.monotonic()
        for cam_id in camera_ids:
            start_worker(cam_id)
            if stagger > 0:
                await asyncio.sleep(stagger)

        samples: dict[str, list[dict]] = {cid: [] for cid in camera_ids}
        cpu_samples: list[float] = []
        rss_samples: list[float] = []
        try:
            import psutil
            process = psutil.Process()
            process.cpu_percent(interval=None)  # prime the non-blocking counter
        except ImportError:
            process = None

        deadline = time.monotonic() + duration
        while time.monotonic() < deadline:
            await asyncio.sleep(1.0)
            for cid in camera_ids:
                stats = CAMERA_STATS.get(cid)
                if stats:
                    samples[cid].append(dict(stats))
            if process is not None:
                cpu_samples.append(process.cpu_percent(interval=None))
                rss_samples.append(process.memory_info().rss / (1024 * 1024))

        wall_seconds = time.monotonic() - started_at
    finally:
        for cid in camera_ids:
            task = stop_worker(cid)
            if task is not None:
                try:
                    await asyncio.wait_for(asyncio.shield(task), timeout=5.0)
                except (asyncio.CancelledError, asyncio.TimeoutError):
                    pass
        db.query(models.Camera).filter(models.Camera.id.in_(camera_ids)).delete(synchronize_session=False)
        db.commit()
        db.close()

    fps_values, inference_ms_values, read_ms_values = [], [], []
    cameras_never_reported = 0
    for cid, rows in samples.items():
        reported = [r for r in rows if r.get("loop_gap_ms_ema")]
        if not reported:
            cameras_never_reported += 1
            continue
        for r in reported:
            gap = r.get("loop_gap_ms_ema")
            if gap:
                fps_values.append(1000.0 / gap)
            if r.get("inference_ms_ema") is not None:
                inference_ms_values.append(r["inference_ms_ema"])
            if r.get("read_ms_ema") is not None:
                read_ms_values.append(r["read_ms_ema"])

    return {
        "cameras_requested": count,
        "cameras_never_reported_a_frame": cameras_never_reported,
        "duration_seconds": round(wall_seconds, 1),
        "fps_mean": round(statistics.mean(fps_values), 2) if fps_values else None,
        "fps_p95_low": round(_percentile(fps_values, 5), 2) if fps_values else None,  # 5th pct = the WORST-case-ish tail for a rate metric
        "inference_ms_mean": round(statistics.mean(inference_ms_values), 1) if inference_ms_values else None,
        "inference_ms_p95": round(_percentile(inference_ms_values, 95), 1) if inference_ms_values else None,
        "read_ms_mean": round(statistics.mean(read_ms_values), 1) if read_ms_values else None,
        "process_cpu_percent_mean": round(statistics.mean(cpu_samples), 1) if cpu_samples else None,
        "process_rss_mb_mean": round(statistics.mean(rss_samples), 1) if rss_samples else None,
        "process_rss_mb_max": round(max(rss_samples), 1) if rss_samples else None,
    }


def _write_markdown(results: list[dict], path: Path) -> None:
    lines = [
        "# SENTINEL VISION camera capacity benchmark",
        "",
        "Measured on THIS machine only, against the bundled demo video decoded",
        "N times concurrently — not N distinct real RTSP streams, not a different",
        "host. Do not generalize beyond what is stated here.",
        "",
        "| Cameras | Never reported | FPS mean | FPS p5 (worst-case tail) | Inference ms mean | Inference ms p95 | CPU % mean | RSS MB mean | RSS MB max |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for r in results:
        lines.append(
            f"| {r['cameras_requested']} | {r['cameras_never_reported_a_frame']} | {r['fps_mean']} | "
            f"{r['fps_p95_low']} | {r['inference_ms_mean']} | {r['inference_ms_p95']} | "
            f"{r['process_cpu_percent_mean']} | {r['process_rss_mb_mean']} | {r['process_rss_mb_max']} |"
        )
    lines.append("")
    lines.append(
        "A stage where `Never reported` > 0 means that many cameras never produced "
        "a single frame-processing sample in the measurement window — a real capacity "
        "ceiling signal, not noise to average away."
    )
    path.write_text("\n".join(lines), encoding="utf-8")


async def main_async(stages: list[int], duration: float, stagger: float) -> list[dict]:
    from app import models  # noqa: F401 — import registers every table on Base.metadata;
    # without it create_all() below sees an empty metadata and creates nothing.
    from app.db import Base, engine, SessionLocal

    if not DEMO_VIDEO.exists():
        print(f"Demo video not found at {DEMO_VIDEO} — cannot run a real workload.", file=sys.stderr)
        return []
    Base.metadata.create_all(bind=engine)

    results = []
    for count in stages:
        print(f"\n=== stage: {count} camera(s), {duration}s ===", file=sys.stderr)
        result = await _run_stage(SessionLocal, count, duration, stagger)
        results.append(result)
        print(json.dumps(result, indent=2), file=sys.stderr)
    return results


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--stages", default="1,3,5", help="comma-separated camera counts to test, e.g. 1,3,5,8,10")
    parser.add_argument("--duration", type=float, default=20.0, help="seconds to measure per stage")
    parser.add_argument("--stagger", type=float, default=1.0, help="seconds between starting each camera in a stage")
    parser.add_argument("--json", type=Path, default=None)
    parser.add_argument("--md", type=Path, default=None)
    args = parser.parse_args()

    stages = [int(s.strip()) for s in args.stages.split(",") if s.strip()]
    results = asyncio.run(main_async(stages, args.duration, args.stagger))
    if not results:
        return 1

    if args.json:
        args.json.write_text(json.dumps(results, indent=2), encoding="utf-8")
        print(f"wrote {args.json}")
    if args.md:
        _write_markdown(results, args.md)
        print(f"wrote {args.md}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
