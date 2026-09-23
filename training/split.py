"""Vehicle-disjoint dataset splitting.

The one rule, and the reason this file exists:

    ONE vehicle identity  ->  ONE split. Always.

Consecutive frames of a vehicle are near-duplicates. Splitting on frames puts an
image of the *same plate, same lighting, same angle* into both train and test,
and the model is then scored on its own training data. That is the standard way
an ANPR accuracy number becomes fiction, and it is undetectable from the number
itself — it just looks like a very good model.

Assignment is by STABLE HASH of the identity, not by shuffling
-------------------------------------------------------------
`hash(f"{seed}:{identity}")` mapped onto the split ratios, rather than
`random.shuffle` over a list. Two properties follow, both of which matter:

1. **Order independence.** The result cannot depend on dict/set iteration
   order, or on the order records happen to appear in the file. Python's
   `hash()` is salted per process, so `hashlib.sha256` is used instead — a
   `random.shuffle` seeded the same way would still be stable, but only if the
   input list order were, which is exactly the assumption that silently breaks.

2. **Growth stability.** Adding 500 new vehicles next month leaves every
   existing vehicle in the split it was already in. With shuffling, appending
   one record reshuffles everything, the test set silently changes, and results
   measured before and after are no longer comparable — while still looking
   like they are.

Ratios are therefore approximate on small corpora: a hash assigns each identity
independently, so a 70/15/15 request over 20 vehicles will not land exactly on
14/3/3. That is the honest trade for stability, and `plan_split` reports the
achieved counts so the deviation is visible rather than assumed away.
"""
from __future__ import annotations

import hashlib
from collections import Counter, defaultdict
from dataclasses import dataclass, field

from schema import PlateRecord, VALID_SPLITS, bucket_for


@dataclass(frozen=True)
class SplitConfig:
    """Everything needed to reproduce a split exactly. Recorded in the manifest
    alongside the dataset version and code revision."""
    seed: str = "sentinel-anpr-v1"
    train: float = 0.70
    val: float = 0.15
    test: float = 0.15
    # The record attribute that defines identity. `vehicle_id` is the default
    # and the correct choice; `plate_text` is offered because a corpus lacking
    # reliable vehicle ids can still be split safely on the registration.
    identity_field: str = "vehicle_id"

    def __post_init__(self) -> None:
        total = self.train + self.val + self.test
        if abs(total - 1.0) > 1e-9:
            raise ValueError(f"split ratios must sum to 1.0, got {total}")
        if min(self.train, self.val, self.test) < 0:
            raise ValueError("split ratios must be non-negative")


@dataclass
class SplitReport:
    config: SplitConfig
    unique_identities: dict[str, int] = field(default_factory=dict)
    records: dict[str, int] = field(default_factory=dict)
    unique_plates: dict[str, int] = field(default_factory=dict)
    images: dict[str, int] = field(default_factory=dict)
    cameras: dict[str, int] = field(default_factory=dict)
    character_counts: dict[str, Counter] = field(default_factory=dict)
    size_buckets: dict[str, Counter] = field(default_factory=dict)

    def identity_percentages(self) -> dict[str, float]:
        total = sum(self.unique_identities.values()) or 1
        return {name: round(100 * count / total, 2) for name, count in self.unique_identities.items()}

    def render(self) -> str:
        lines = [
            "# Split report",
            "",
            f"seed: `{self.config.seed}`  |  identity field: `{self.config.identity_field}`",
            f"requested ratios: train {self.config.train:.2f} / val {self.config.val:.2f} / test {self.config.test:.2f}",
            "",
            "| split | vehicles | % of vehicles | records | unique plates | images | cameras |",
            "|---|---|---|---|---|---|---|",
        ]
        percentages = self.identity_percentages()
        for name in VALID_SPLITS:
            lines.append(
                f"| {name} | {self.unique_identities.get(name, 0)} | {percentages.get(name, 0.0)} "
                f"| {self.records.get(name, 0)} | {self.unique_plates.get(name, 0)} "
                f"| {self.images.get(name, 0)} | {self.cameras.get(name, 0)} |"
            )
        lines += ["", "## Plate-size distribution by split", "",
                  "| split | " + " | ".join(b[0] for b in _bucket_names()) + " |",
                  "|---" * (1 + len(_bucket_names())) + "|"]
        for name in VALID_SPLITS:
            counts = self.size_buckets.get(name, Counter())
            lines.append(f"| {name} | " + " | ".join(str(counts.get(b[0], 0)) for b in _bucket_names()) + " |")
        return "\n".join(lines) + "\n"


def _bucket_names():
    from schema import DEFAULT_SIZE_BUCKETS
    return DEFAULT_SIZE_BUCKETS


def identity_of(record: PlateRecord, config: SplitConfig) -> str:
    return str(getattr(record, config.identity_field, "") or "")


def assign_identity(identity: str, config: SplitConfig) -> str:
    """Deterministically place one identity into a split.

    SHA-256 of `seed:identity` gives a uniform fraction in [0, 1); the split
    ratios partition that interval. `hashlib` rather than the builtin `hash()`
    because the latter is salted per process (PYTHONHASHSEED) and would produce
    a different split on every run.
    """
    digest = hashlib.sha256(f"{config.seed}:{identity}".encode("utf-8")).digest()
    # 8 bytes is ample resolution and keeps the arithmetic in native int range.
    fraction = int.from_bytes(digest[:8], "big") / float(1 << 64)
    if fraction < config.train:
        return "train"
    if fraction < config.train + config.val:
        return "val"
    return "test"


def assign_splits(records: list[PlateRecord], config: SplitConfig | None = None) -> list[PlateRecord]:
    """Set `.split` on every record, in place, by its identity.

    Records sharing an identity are guaranteed the same split because the split
    is a pure function of the identity string — there is no per-record
    randomness that could separate them.

    Records with an EMPTY identity are left unassigned (`split=""`) rather than
    being defaulted into train: an observation whose vehicle is unknown cannot
    be guaranteed disjoint from anything, so it must not silently become
    training data. `qc.checks` reports them.
    """
    config = config or SplitConfig()
    for record in records:
        identity = identity_of(record, config)
        record.split = assign_identity(identity, config) if identity else ""
    return records


def plan_split(records: list[PlateRecord], config: SplitConfig | None = None) -> SplitReport:
    """Summarize an already-assigned split. Does not modify records."""
    config = config or SplitConfig()
    report = SplitReport(config=config)
    identities: dict[str, set[str]] = defaultdict(set)
    plates: dict[str, set[str]] = defaultdict(set)
    images: dict[str, set[str]] = defaultdict(set)
    cameras: dict[str, set[str]] = defaultdict(set)
    characters: dict[str, Counter] = defaultdict(Counter)
    buckets: dict[str, Counter] = defaultdict(Counter)
    counts: Counter = Counter()

    for record in records:
        split = record.split or "unassigned"
        counts[split] += 1
        identities[split].add(identity_of(record, config))
        if record.plate_text:
            plates[split].add(record.plate_text)
            characters[split].update(record.plate_text)
        images[split].add(record.image_id)
        cameras[split].add(record.camera_id)
        buckets[split][bucket_for(record.effective_glyph_px())] += 1

    for split in set(list(counts) + list(VALID_SPLITS)):
        report.unique_identities[split] = len(identities.get(split, set()))
        report.records[split] = counts.get(split, 0)
        report.unique_plates[split] = len(plates.get(split, set()))
        report.images[split] = len(images.get(split, set()))
        report.cameras[split] = len(cameras.get(split, set()))
        report.character_counts[split] = characters.get(split, Counter())
        report.size_buckets[split] = buckets.get(split, Counter())
    return report


def find_identity_leakage(
    records: list[PlateRecord], config: SplitConfig | None = None,
) -> dict[str, set[str]]:
    """Identities appearing in more than one split — the fatal case.

    Returns `{identity: {splits}}`, empty when clean. This is what the CI gate
    asserts on.
    """
    config = config or SplitConfig()
    seen: dict[str, set[str]] = defaultdict(set)
    for record in records:
        identity = identity_of(record, config)
        if identity and record.split:
            seen[identity].add(record.split)
    return {identity: splits for identity, splits in seen.items() if len(splits) > 1}


def find_image_leakage(records: list[PlateRecord]) -> dict[str, set[str]]:
    """The same `image_id` in more than one split.

    Distinct from identity leakage: one frame can legitimately contain two
    vehicles, and if those two vehicles hash into different splits the FRAME is
    in both. That is a real leak — the same pixels are in train and test — even
    though no identity crosses. Reported separately because the fix differs:
    identity leakage is a splitter bug, image leakage is a decision about
    multi-vehicle frames.
    """
    seen: dict[str, set[str]] = defaultdict(set)
    for record in records:
        if record.split:
            seen[record.image_id].add(record.split)
    return {image: splits for image, splits in seen.items() if len(splits) > 1}


def find_repeated_plate_text(records: list[PlateRecord]) -> dict[str, set[str]]:
    """Plate strings appearing under more than one `vehicle_id`.

    **Reported, never auto-failed.** Two records with the same registration are
    usually the same vehicle given two ids by mistake — but not always: a
    transcription can legitimately coincide (a partial label like `KL34F`), and
    cloned or misread plates exist in real traffic. Treating this as leakage
    automatically would delete genuine data; treating it as invisible would hide
    a real identity bug. So it is surfaced for a human.
    """
    by_text: dict[str, set[str]] = defaultdict(set)
    for record in records:
        if record.plate_text:
            by_text[record.plate_text].add(record.vehicle_id)
    return {text: ids for text, ids in by_text.items() if len(ids) > 1}
