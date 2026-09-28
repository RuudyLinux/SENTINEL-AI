"""Convert Pascal VOC plate annotations into the ANPR benchmark corpus layout.

tools/anpr_bench.py takes ground truth from the filename (stem up to the
first underscore). CVAT, makesense.ai, LabelImg and most plate datasets emit
Pascal VOC XML with the text in <attributes>; this bridges them.

Input (either naming works):

    <root>/images/*.jpg        or  <root>/JPEGImages/*.jpg
    <root>/annotations/*.xml   or  <root>/Annotations/*.xml

Plate text per <object>:

    <attributes><attribute>
        <name>number_plate_text</name><value>GJ01DY6855</value>
    </attribute></attributes>

Objects without text are skipped for the recognition corpus but counted and
reported; how many plates are transcribed at all is part of the result.

It crops instead of copying whole images because the live pipeline never
gives OCR a full frame: worker._run_anpr passes a vehicle crop to
plate_detect.locate_plate. Each plate is exported with --context (default
2.5x) around it, roughly the vehicle crop the localizer gets, so both stages
are tested. --tight exports just the plate box, OCR only; run both to tell
"localizer missed" from "OCR misread".

    python tools/anpr_corpus_from_voc.py <root> --out tools/anpr_corpus
    python tools/anpr_corpus_from_voc.py <root> --out /tmp/tight --tight
"""
from __future__ import annotations

import argparse
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

TEXT_ATTRIBUTE = "number_plate_text"
IMAGE_SUFFIXES = (".jpg", ".jpeg", ".png", ".bmp", ".webp")


def _find_dir(root: Path, *names: str) -> "Path | None":
    for name in names:
        candidate = root / name
        if candidate.is_dir():
            return candidate
    return None


def _plate_text(obj: ET.Element) -> str:
    for attr in obj.findall(".//attribute"):
        if (attr.findtext("name") or "").strip() == TEXT_ATTRIBUTE:
            return (attr.findtext("value") or "").strip().upper()
    return ""


def _bbox(obj: ET.Element) -> "tuple[float, float, float, float] | None":
    box = obj.find("bndbox")
    if box is None:
        return None
    try:
        return (
            float(box.findtext("xmin")), float(box.findtext("ymin")),
            float(box.findtext("xmax")), float(box.findtext("ymax")),
        )
    except (TypeError, ValueError):
        return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("root", type=Path, help="dataset root containing images/ and annotations/")
    parser.add_argument("--out", type=Path, required=True, help="corpus directory to write crops into")
    parser.add_argument("--context", type=float, default=2.5,
                        help="expand the plate box by this factor to approximate a vehicle crop (default 2.5)")
    parser.add_argument("--tight", action="store_true", help="export the plate box only (isolates OCR from localization)")
    args = parser.parse_args()

    import cv2

    images_dir = _find_dir(args.root, "images", "JPEGImages", "Images")
    annotations_dir = _find_dir(args.root, "annotations", "Annotations")
    if images_dir is None or annotations_dir is None:
        print(f"could not find images/ and annotations/ under {args.root}", file=sys.stderr)
        return 2

    args.out.mkdir(parents=True, exist_ok=True)
    total_objects = labelled = written = 0
    skipped_unreadable = 0
    seen: dict[str, int] = {}

    for xml_path in sorted(annotations_dir.glob("*.xml")):
        stem = xml_path.stem
        image_path = next(
            (p for suffix in IMAGE_SUFFIXES if (p := images_dir / f"{stem}{suffix}").exists()), None
        )
        if image_path is None:
            continue
        frame = cv2.imread(str(image_path))
        if frame is None:
            skipped_unreadable += 1
            continue
        height, width = frame.shape[:2]

        for obj in ET.parse(xml_path).getroot().findall("object"):
            total_objects += 1
            text = _plate_text(obj)
            box = _bbox(obj)
            if not text or box is None:
                continue
            # Only alphanumerics survive into the filename; the benchmark
            # normalizes the same way, so the comparison stays apples-to-apples.
            label = re.sub(r"[^A-Z0-9]", "", text)
            if not label:
                continue
            labelled += 1

            x1, y1, x2, y2 = box
            if not args.tight:
                cx, cy = (x1 + x2) / 2, (y1 + y2) / 2
                half_w = (x2 - x1) * args.context / 2
                half_h = (y2 - y1) * args.context / 2
                x1, y1, x2, y2 = cx - half_w, cy - half_h, cx + half_w, cy + half_h
            xi1, yi1 = max(0, int(x1)), max(0, int(y1))
            xi2, yi2 = min(width, int(x2)), min(height, int(y2))
            if xi2 - xi1 < 8 or yi2 - yi1 < 8:
                continue
            crop = frame[yi1:yi2, xi1:xi2]
            if crop.size == 0:
                continue

            # the same plate can be labelled in several images (or twice in
            # one); each is its own sample, suffixed so ground truth parses
            seen[label] = seen.get(label, 0) + 1
            out_name = f"{label}_{stem}-{seen[label]}.jpg"
            cv2.imwrite(str(args.out / out_name), crop)
            written += 1

    print(f"plate objects found:      {total_objects}")
    print(f"objects WITH a text label: {labelled}")
    print(f"corpus samples written:    {written} -> {args.out}")
    if skipped_unreadable:
        print(f"unreadable images skipped: {skipped_unreadable}")
    if total_objects and labelled < total_objects:
        print(f"\nNOTE: {total_objects - labelled} of {total_objects} plate objects carry NO text label and "
              "cannot be scored for recognition accuracy — only for localization.")
    return 0 if written else 1


if __name__ == "__main__":
    raise SystemExit(main())
