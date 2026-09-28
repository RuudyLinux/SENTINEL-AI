"""Vehicle-disjoint dataset splitting.

    ONE vehicle identity  ->  ONE split. Always.

Consecutive frames of a vehicle are near-duplicates. Split on frames and the
same plate, light and angle is in train and test, and the model is scored on
its training data. That's the usual way ANPR accuracy numbers become fiction,
and the number itself can't tell you.

Assignment is a stable hash of the identity, not a shuffle:
1. Order independent: doesn't depend on dict/set order or file order.
   Python's hash() is salted per process, hence sha256.
2. Growth stable: adding 500 vehicles next month leaves existing ones where
   they were. With shuffling one new record reshuffles everything and the
   test set silently changes.

So ratios are approximate on small corpora (70/15/15 over 20 vehicles won't
be exactly 14/3/3); plan_split reports the achieved counts.
"""
from __future__ import annotations

import hashlib
from collections import Counter, defaultdict
from dataclasses import dataclass, field

from schema import PlateRecord, VALID_SPLITS, bucket_for


@dataclass(frozen=True)
class SplitConfig:
    """Everything needed to reproduce a split. Goes in the manifest with the
    dataset version and code revision."""
    seed: str = "sentinel-anpr-v1"
    train: float = 0.70
    val: float = 0.15
    test: float = 0.15
    # identity attribute. vehicle_id is the right default; plate_text is for a
    # corpus without reliable vehicle ids
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
    """Place one identity in a split: sha256(seed:identity) as a fraction in
    [0, 1), partitioned by the ratios. hashlib because builtin hash() is
    salted per process (PYTHONHASHSEED).
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
    """Set .split on every record in place by its identity. Same identity,
    same split, since it's a pure function of the identity.

    Empty identity stays unassigned (split="") instead of defaulting to
    train: an unknown vehicle can't be guaranteed disjoint, so it mustn't
    quietly become training data. qc.checks reports them.
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
    """Identities in more than one split, the fatal case. {identity: {splits}},
    empty when clean. The CI gate asserts on this.
    """
    config = config or SplitConfig()
    seen: dict[str, set[str]] = defaultdict(set)
    for record in records:
        identity = identity_of(record, config)
        if identity and record.split:
            seen[identity].add(record.split)
    return {identity: splits for identity, splits in seen.items() if len(splits) > 1}


def find_image_leakage(records: list[PlateRecord]) -> dict[str, set[str]]:
    """The same image_id in more than one split.

    Not identity leakage: one frame can hold two vehicles that hash to
    different splits, and then the same pixels are in both. Reported
    separately because the fix differs (splitter bug vs a decision about
    multi-vehicle frames).
    """
    seen: dict[str, set[str]] = defaultdict(set)
    for record in records:
        if record.split:
            seen[record.image_id].add(record.split)
    return {image: splits for image, splits in seen.items() if len(splits) > 1}


def find_repeated_plate_text(records: list[PlateRecord]) -> dict[str, set[str]]:
    """Plate strings under more than one vehicle_id. Reported, never
    auto-failed: usually one vehicle given two ids by mistake, but partial
    labels (KL34F) can coincide and cloned or misread plates exist. A human
    decides.
    """
    by_text: dict[str, set[str]] = defaultdict(set)
    for record in records:
        if record.plate_text:
            by_text[record.plate_text].add(record.vehicle_id)
    return {text: ids for text, ids in by_text.items() if len(ids) > 1}
