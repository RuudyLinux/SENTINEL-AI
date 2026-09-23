"""Dataset reports: QC summary, character coverage, plate-size distribution.

The character-coverage report exists because of a specific measured failure. The
current n=25 benchmark corpus contains **zero** instances of `I O Q V Z`, and
twelve more classes appear at most twice — a fact that was invisible until it
was counted, and that silently caps what any recogniser trained on such a corpus
could learn. This report makes that visible on day one of the next dataset
rather than after a training run.

All output is Markdown, so a report can be committed next to the dataset card
and diffed between dataset versions.
"""
from __future__ import annotations

from collections import Counter, defaultdict

from schema import DEFAULT_SIZE_BUCKETS, PlateRecord, VALID_SPLITS, bucket_for
from qc.checks import ERROR, INFO, WARNING, Finding

DIGITS = "0123456789"
LETTERS = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
ALPHABET = DIGITS + LETTERS

# Characters known to be rare in Indian registrations. `I` and `O` are avoided
# by design in many series precisely because they collide with `1` and `0`, so
# they will stay rare in any naturally collected corpus and need deliberate
# attention (synthetic top-up) rather than being assumed to arrive on their own.
KNOWN_RARE = ("I", "O", "Q", "V", "Z", "F", "X")

# Coverage targets from docs/ANPR_M0_DATA_ACQUISITION.md §9 (pilot tier).
PILOT_TRAIN_MIN_COMMON = 300
PILOT_TRAIN_MIN_RARE = 100
PILOT_TEST_MIN = 20


def character_coverage(records: list[PlateRecord]) -> dict[str, dict]:
    """Per-character occurrence, unique plates, unique vehicles, and per-split
    counts."""
    occurrences: Counter = Counter()
    plates: dict[str, set[str]] = defaultdict(set)
    vehicles: dict[str, set[str]] = defaultdict(set)
    per_split: dict[str, Counter] = defaultdict(Counter)

    for record in records:
        text = record.plate_text
        if not text:
            continue
        occurrences.update(text)
        for character in set(text):
            plates[character].add(text)
            vehicles[character].add(record.vehicle_id)
            if record.split:
                per_split[character][record.split] += 1

    return {
        character: {
            "occurrences": occurrences.get(character, 0),
            "unique_plates": len(plates.get(character, set())),
            "unique_vehicles": len(vehicles.get(character, set())),
            "train": per_split.get(character, Counter()).get("train", 0),
            "val": per_split.get(character, Counter()).get("val", 0),
            "test": per_split.get(character, Counter()).get("test", 0),
        }
        for character in ALPHABET
    }


def render_character_coverage(records: list[PlateRecord]) -> str:
    coverage = character_coverage(records)
    missing = [c for c in ALPHABET if coverage[c]["occurrences"] == 0]
    scarce = [c for c in ALPHABET if 0 < coverage[c]["occurrences"] <= 2]

    lines = [
        "# Character coverage",
        "",
        f"total character instances: **{sum(v['occurrences'] for v in coverage.values())}**  |  "
        f"classes present: **{36 - len(missing)}/36**",
        "",
    ]
    if missing:
        lines += [
            f"> **ZERO COVERAGE ({len(missing)}): `{' '.join(missing)}`** — a recogniser cannot "
            "learn a character it has never seen. Fill from synthetic data (never by duplicating "
            "real plates, which teaches the plate rather than the glyph).",
            "",
        ]
    else:
        lines += ["> All 36 classes present.", ""]
    if scarce:
        lines += [f"> Fewer than 3 occurrences: `{' '.join(scarce)}`", ""]

    lines += [
        "| char | occurrences | unique plates | unique vehicles | train | val | test | status |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for character in ALPHABET:
        row = coverage[character]
        target = PILOT_TRAIN_MIN_RARE if character in KNOWN_RARE else PILOT_TRAIN_MIN_COMMON
        if row["occurrences"] == 0:
            status = "**MISSING**"
        elif row["train"] < target:
            status = f"under target ({row['train']}/{target} train)"
        elif row["test"] < PILOT_TEST_MIN:
            status = f"test thin ({row['test']}/{PILOT_TEST_MIN})"
        else:
            status = "ok"
        lines.append(
            f"| {character} | {row['occurrences']} | {row['unique_plates']} | {row['unique_vehicles']} "
            f"| {row['train']} | {row['val']} | {row['test']} | {status} |"
        )
    lines += [
        "",
        f"Targets (pilot): common classes ≥{PILOT_TRAIN_MIN_COMMON} train, "
        f"rare classes (`{' '.join(KNOWN_RARE)}`) ≥{PILOT_TRAIN_MIN_RARE} train, "
        f"all classes ≥{PILOT_TEST_MIN} test.",
        "",
        "A class below its **test** minimum is reported as *not evaluated* for that character, "
        "never averaged silently into the aggregate.",
    ]
    return "\n".join(lines) + "\n"


def size_distribution(
    records: list[PlateRecord],
    predictions: dict[str, str] | None = None,
    buckets=DEFAULT_SIZE_BUCKETS,
) -> dict[str, dict]:
    """Count, percentage and — when predictions exist — exact match per bucket.

    `predictions` maps `image_id/vehicle_id` to the predicted string. Absent, the
    accuracy columns are simply not produced: this reports what is measured and
    nothing else.
    """
    per_bucket: dict[str, dict] = {
        name: {"count": 0, "correct": 0, "scored": 0} for name, _, _ in buckets
    }
    for record in records:
        name = bucket_for(record.effective_glyph_px(), buckets)
        per_bucket[name]["count"] += 1
        if predictions is not None:
            key = f"{record.image_id}/{record.vehicle_id}"
            if key in predictions and record.plate_text:
                per_bucket[name]["scored"] += 1
                if predictions[key].upper() == record.plate_text.upper():
                    per_bucket[name]["correct"] += 1

    total = sum(entry["count"] for entry in per_bucket.values()) or 1
    for entry in per_bucket.values():
        entry["percentage"] = round(100 * entry["count"] / total, 2)
        entry["exact_match"] = (
            round(entry["correct"] / entry["scored"], 4) if entry["scored"] else None
        )
    return per_bucket


def render_size_distribution(
    records: list[PlateRecord], predictions: dict[str, str] | None = None,
) -> str:
    distribution = size_distribution(records, predictions)
    lines = [
        "# Plate-size distribution",
        "",
        "Bucketed on **character height in pixels** — glyph height is what governs legibility "
        "and is what the recogniser's input resize is defined against.",
        "",
        "| bucket | count | % | scored | exact match |",
        "|---|---|---|---|---|",
    ]
    for name, _, _ in DEFAULT_SIZE_BUCKETS:
        entry = distribution[name]
        exact = "—" if entry["exact_match"] is None else f"{entry['exact_match']:.4f}"
        lines.append(
            f"| {name} | {entry['count']} | {entry['percentage']} | {entry['scored']} | {exact} |"
        )
    lines += [
        "",
        "An aggregate accuracy on a corpus weighted toward large plates does not answer whether "
        "the recogniser works at CCTV resolution. That is what this table is for.",
    ]
    return "\n".join(lines) + "\n"


def render_qc_report(findings: list[Finding]) -> str:
    by_severity = Counter(f.severity for f in findings)
    by_code: dict[str, list[Finding]] = defaultdict(list)
    for finding in findings:
        by_code[finding.code].append(finding)

    lines = [
        "# QC report",
        "",
        f"**{by_severity.get(ERROR, 0)} errors**, {by_severity.get(WARNING, 0)} warnings, "
        f"{by_severity.get(INFO, 0)} info",
        "",
    ]
    if by_severity.get(ERROR, 0):
        lines += ["> **Dataset is BLOCKED.** Errors must be resolved before training.", ""]
    else:
        lines += ["> No blocking errors.", ""]

    lines += ["| severity | code | count |", "|---|---|---|"]
    order = {ERROR: 0, WARNING: 1, INFO: 2}
    for code, items in sorted(by_code.items(), key=lambda kv: (order.get(kv[1][0].severity, 9), kv[0])):
        lines.append(f"| {items[0].severity} | `{code}` | {len(items)} |")

    for severity in (ERROR, WARNING):
        items = [f for f in findings if f.severity == severity]
        if not items:
            continue
        lines += ["", f"## {severity}S", ""]
        for finding in items[:100]:
            lines.append(f"- {finding}")
        if len(items) > 100:
            lines.append(f"- …and {len(items) - 100} more")
    return "\n".join(lines) + "\n"
