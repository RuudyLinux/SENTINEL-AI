"""ANPR benchmark harness — measure before replacing anything.

The V2 brief asks whether EasyOCR should be replaced (e.g. by PaddleOCR). That
question cannot be answered honestly without numbers on real frames, and the
answer is very likely to change now that plate LOCALIZATION exists: pre-V2, OCR
was handed a whole vehicle crop, so a large share of the failures were "the OCR
engine was pointed at a car" rather than "the OCR engine is weak". Swapping
engines before measuring that would attribute the localization win to the new
engine.

This tool therefore compares CONFIGURATIONS, not just engines:

    whole-crop + EasyOCR     (the pre-V2 pipeline)
    localized + EasyOCR      (V2 today)
    localized + <engine>     (a candidate, if installed)

Usage
-----
Put labelled images in a corpus directory. The ground-truth plate is the
filename stem, uppercase, non-alphanumerics stripped:

    corpus/
      GJ05AB1234.jpg          # a vehicle crop, or a full frame
      GJ01XY7788_night.jpg    # text after the first "_" is ignored
      GJ18CD4455_blur.jpg

    python tools/anpr_bench.py corpus/ --json results.json

What it reports
---------------
Exact-match rate, character error rate, mean confidence, and wall time per
image, per configuration. It deliberately does NOT print a verdict: a 3%
accuracy gain that costs 4x the CPU is a different decision on a 30-camera
box than on a workstation, and that trade is the operator's to make.

Honesty note: with no corpus, this prints nothing and claims nothing. There is
no bundled sample set — real Gujarat plate footage is not something this
repository can ship, and synthetic images would produce a number that looks
like evidence while measuring nothing.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

# Import as a package so this runs from the backend directory.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import cv2  # noqa: E402

from app.pipeline import plate_detect  # noqa: E402
from app.pipeline.anpr import looks_like_plate, normalize_plate  # noqa: E402

IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".bmp", ".webp"}


def ground_truth_from(path: Path) -> str:
    """Filename stem up to the first underscore, normalized like an OCR read."""
    return normalize_plate(re.split(r"[_\-\s]", path.stem)[0].upper())


def levenshtein(a: str, b: str) -> int:
    if not a:
        return len(b)
    if not b:
        return len(a)
    previous = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        current = [i]
        for j, cb in enumerate(b, 1):
            current.append(min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + (ca != cb)))
        previous = current
    return previous[-1]


@dataclass
class Result:
    name: str
    total: int = 0
    exact: int = 0
    plausible: int = 0
    char_errors: int = 0
    char_total: int = 0
    confidence_sum: float = 0.0
    seconds: float = 0.0
    misses: list[tuple[str, str]] = field(default_factory=list)

    def record(self, truth: str, read: str, confidence: float, elapsed: float) -> None:
        self.total += 1
        self.seconds += elapsed
        self.confidence_sum += confidence
        self.char_errors += levenshtein(truth, read)
        self.char_total += len(truth)
        if read == truth:
            self.exact += 1
        else:
            self.misses.append((truth, read))
        if looks_like_plate(read):
            self.plausible += 1

    def as_dict(self) -> dict:
        n = max(1, self.total)
        return {
            "configuration": self.name,
            "images": self.total,
            "exact_match_rate": round(self.exact / n, 4),
            # A read that is wrong but plate-SHAPED is the dangerous kind: it
            # passes the quality gate and becomes a real vehicle record.
            "plausible_format_rate": round(self.plausible / n, 4),
            "character_error_rate": round(self.char_errors / max(1, self.char_total), 4),
            "mean_confidence": round(self.confidence_sum / n, 4),
            "mean_seconds_per_image": round(self.seconds / n, 4),
        }


def _read_easyocr(image):
    from app.pipeline.anpr import read_plate

    _, normalized, confidence = read_plate(image)
    return normalized, confidence


def _read_paddleocr(image):
    """Candidate engine. Returns None when PaddleOCR is not installed, so the
    benchmark simply omits that row rather than failing or faking it."""
    try:
        from paddleocr import PaddleOCR
    except ImportError:
        return None
    global _PADDLE
    try:
        _PADDLE
    except NameError:
        _PADDLE = PaddleOCR(use_angle_cls=True, lang="en", show_log=False)
    result = _PADDLE.ocr(image, cls=True)
    if not result or not result[0]:
        return "", 0.0
    fragments = [(line[1][0], float(line[1][1])) for line in result[0]]
    text = "".join(f[0] for f in fragments)
    confidence = sum(f[1] for f in fragments) / len(fragments)
    return normalize_plate(text), confidence


def _read_easyocr_with_fallback(image):
    """The CURRENT pipeline strategy: read the localized region, and if that
    read fails the quality gate, re-read the whole crop and keep the better of
    the two (see anpr.better_read). Measured because "localized" alone was
    found to REDUCE accuracy on real plates — this is the fix, so the
    benchmark has to be able to score it."""
    from app.pipeline.anpr import better_read, passes_anpr_gate, read_plate
    from app.pipeline import plate_detect

    located = plate_detect.locate_plate(image)
    if located is None:
        _, normalized, confidence = read_plate(image)
        return normalized, confidence
    _, normalized, confidence = read_plate(located[0])
    if not passes_anpr_gate(normalized, confidence):
        best = better_read(("", normalized, confidence), read_plate(image))
        normalized, confidence = best[1], best[2]
    return normalized, confidence


CONFIGURATIONS = [
    ("whole-crop + easyocr", _read_easyocr, False),
    ("localized + easyocr", _read_easyocr, True),
    # `localize=False` because this configuration does its OWN localization
    # internally (it needs the un-cropped image to fall back to).
    ("localized+fallback + easyocr", _read_easyocr_with_fallback, False),
    ("localized + paddleocr", _read_paddleocr, True),
]


def run(corpus: Path) -> list[dict]:
    images = sorted(p for p in corpus.iterdir() if p.suffix.lower() in IMAGE_SUFFIXES)
    if not images:
        print(f"No images found in {corpus} — nothing to measure.", file=sys.stderr)
        return []

    results: list[Result] = []
    for name, reader, localize in CONFIGURATIONS:
        probe = reader(cv2.imread(str(images[0])))
        if probe is None:
            print(f"skipping '{name}' — engine not installed", file=sys.stderr)
            continue

        result = Result(name=name)
        for path in images:
            truth = ground_truth_from(path)
            if not truth:
                continue
            frame = cv2.imread(str(path))
            if frame is None:
                print(f"skipping unreadable image {path.name}", file=sys.stderr)
                continue

            started = time.monotonic()
            target = frame
            if localize:
                located = plate_detect.locate_plate(frame)
                # A localization miss falls back to the whole crop — exactly
                # what the live pipeline does, so the measurement reflects real
                # behavior rather than an idealized one.
                if located is not None:
                    target = located[0]
            read, confidence = reader(target)
            result.record(truth, read, confidence, time.monotonic() - started)
        results.append(result)

    for result in results:
        summary = result.as_dict()
        print(f"\n{summary['configuration']}")
        for key, value in summary.items():
            if key != "configuration":
                print(f"  {key:28} {value}")
        if result.misses:
            print(f"  {'sample misses':28} " + ", ".join(f"{t}->{r or '<empty>'}" for t, r in result.misses[:5]))

    return [r.as_dict() for r in results]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("corpus", type=Path, help="directory of labelled plate images")
    parser.add_argument("--json", type=Path, help="also write the results to this file")
    args = parser.parse_args()

    if not args.corpus.is_dir():
        print(f"{args.corpus} is not a directory", file=sys.stderr)
        return 2

    results = run(args.corpus)
    if args.json and results:
        args.json.write_text(json.dumps(results, indent=2))
        print(f"\nwrote {args.json}")
    return 0 if results else 1


if __name__ == "__main__":
    raise SystemExit(main())
