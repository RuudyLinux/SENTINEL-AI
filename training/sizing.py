"""Plate-size statistics: bucketing, percentiles, and sampling-bias control.

The measurement core for M1.5. Deliberately separated from
`analyze_cctv_size.py`, which does the video I/O and detection: everything here
is stdlib-only and operates on plain observation records, so the statistics are
fully testable without footage, without OpenCV and without the inference stack.

The question this exists to answer
----------------------------------
What plate and character pixel sizes will the system actually encounter on the
deployment's cameras? The only distribution measured so far is mobile-phone
photography with a **median glyph height of 75.4px** — far larger than CCTV will
produce, and therefore the wrong basis for choosing a recogniser input size or
an architecture.

Two measurement hazards this module is built around
---------------------------------------------------
**1. Provenance.** A box from the classical detector is not a plate location. On
the labelled benchmark it reached mean IoU 0.136, and 12 of 16 detections landed
on something that was not a plate. Any statistic derived from detector boxes is
therefore labelled `detector-estimated` and never reported as ground truth.

**2. Sampling bias.** A vehicle stationary at a signal for 200 frames would
contribute 200 observations and dominate the distribution — the measurement
would describe that one vehicle rather than the traffic. Vehicle-weighted
sampling caps observations per track, and the report shows both weightings side
by side so the difference is visible rather than assumed away.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field

from schema import DEFAULT_SIZE_BUCKETS, bucket_for, estimate_glyph_px

# Provenance of a plate box. Never mixed in a single reported statistic.
SOURCE_DETECTOR = "detector"
SOURCE_GROUND_TRUTH = "ground_truth"

PERCENTILES = (10, 25, 50, 75, 90, 95)

# Used where metadata genuinely does not say. Never guessed.
UNKNOWN = "unknown"


@dataclass(frozen=True)
class PlateObservation:
    """One observed plate box in one frame.

    `track_id` is what makes vehicle-weighted sampling possible. When the source
    has no tracker, pass a per-frame unique value and accept that
    frame-weighting and vehicle-weighting coincide — the report says so rather
    than implying a vehicle count it does not have.
    """
    camera_id: str
    frame_index: int
    frame_width: int
    frame_height: int
    bbox: tuple[float, float, float, float]
    source: str = SOURCE_DETECTOR
    track_id: str = ""
    row_layout: str = "single"
    vehicle_category: str = UNKNOWN
    plate_face: str = UNKNOWN
    time_of_day: str = UNKNOWN
    detector_confidence: float = 0.0
    vehicle_bbox: "tuple[float, float, float, float] | None" = None
    # Set only when a real character-band measurement exists (an annotation).
    # Overrides the estimate.
    measured_glyph_px: float | None = None

    @property
    def width(self) -> float:
        return self.bbox[2] - self.bbox[0]

    @property
    def height(self) -> float:
        return self.bbox[3] - self.bbox[1]

    @property
    def aspect_ratio(self) -> float:
        return self.width / self.height if self.height else 0.0

    @property
    def glyph_px(self) -> float:
        if self.measured_glyph_px is not None:
            return float(self.measured_glyph_px)
        return estimate_glyph_px(self.height, self.row_layout)

    @property
    def is_estimated_glyph(self) -> bool:
        return self.measured_glyph_px is None

    @property
    def bucket(self) -> str:
        return bucket_for(self.glyph_px)

    @property
    def frame_area_fraction(self) -> float:
        area = self.frame_width * self.frame_height
        return (self.width * self.height) / area if area else 0.0

    @property
    def vehicle_area_fraction(self) -> float | None:
        """Plate area as a fraction of its vehicle crop, when the vehicle box is
        known. None otherwise — never substituted with the frame fraction."""
        if self.vehicle_bbox is None:
            return None
        vw = self.vehicle_bbox[2] - self.vehicle_bbox[0]
        vh = self.vehicle_bbox[3] - self.vehicle_bbox[1]
        area = vw * vh
        return (self.width * self.height) / area if area > 0 else None


# ---- percentiles ---------------------------------------------------------

def percentile(values: list[float], point: float) -> float:
    """Linear-interpolation percentile (the common "type 7" definition).

    Implemented rather than imported so this module stays stdlib-only and its
    definition is pinned — percentile conventions differ between libraries, and
    a silently different definition would make two reports incomparable.
    """
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return float(ordered[0])
    rank = (len(ordered) - 1) * (point / 100.0)
    low = int(rank)
    high = min(low + 1, len(ordered) - 1)
    weight = rank - low
    return float(ordered[low] * (1 - weight) + ordered[high] * weight)


def percentile_summary(values: list[float]) -> dict[str, float]:
    summary = {f"p{p}": round(percentile(values, p), 2) for p in PERCENTILES}
    summary["min"] = round(min(values), 2) if values else 0.0
    summary["max"] = round(max(values), 2) if values else 0.0
    summary["mean"] = round(sum(values) / len(values), 2) if values else 0.0
    summary["n"] = len(values)
    return summary


# ---- sampling ------------------------------------------------------------

def vehicle_weighted(
    observations: list[PlateObservation], max_per_track: int = 3,
) -> list[PlateObservation]:
    """Cap observations per track so one long-dwelling vehicle cannot dominate.

    Selection within a track is by evenly spaced frame index rather than "first
    N": the first N frames of a track are its entry into the scene, all at a
    similar distance, which would bias the size distribution toward whatever
    size a vehicle happens to be when it first appears. Even spacing samples the
    vehicle across its whole pass.

    Deterministic — no randomness — so a report is reproducible from the same
    footage and the same cap.
    """
    if max_per_track <= 0:
        return list(observations)
    by_track: dict[str, list[PlateObservation]] = defaultdict(list)
    for observation in observations:
        by_track[observation.track_id or f"__frame_{observation.frame_index}"].append(observation)

    kept: list[PlateObservation] = []
    for track in sorted(by_track):
        members = sorted(by_track[track], key=lambda o: o.frame_index)
        if len(members) <= max_per_track:
            kept.extend(members)
            continue
        step = (len(members) - 1) / (max_per_track - 1) if max_per_track > 1 else 0
        indices = sorted({int(round(i * step)) for i in range(max_per_track)})
        kept.extend(members[i] for i in indices)
    return sorted(kept, key=lambda o: (o.camera_id, o.frame_index))


def frame_indices(
    total_frames: int, fps: float, sample_fps: float | None = None,
    max_frames: int | None = None, max_seconds: float | None = None,
) -> list[int]:
    """Which frame indices to decode.

    Interval sampling, not random: it is reproducible, it needs no seek-heavy
    random access, and it covers the footage evenly. `sample_fps` expresses the
    interval in a unit an operator can reason about ("two frames a second")
    rather than a stride that depends on the source frame rate.
    """
    if total_frames <= 0:
        return []
    limit = total_frames
    if max_seconds is not None and fps > 0:
        limit = min(limit, int(max_seconds * fps))
    stride = 1
    if sample_fps and fps > 0:
        stride = max(1, int(round(fps / sample_fps)))
    indices = list(range(0, limit, stride))
    if max_frames is not None and len(indices) > max_frames:
        # Thin evenly rather than truncating, so a cap does not silently
        # restrict the measurement to the beginning of the footage.
        step = len(indices) / max_frames
        indices = [indices[int(i * step)] for i in range(max_frames)]
    return indices


# ---- aggregation ---------------------------------------------------------

@dataclass
class SizeDistribution:
    label: str
    counts: Counter = field(default_factory=Counter)
    glyph_values: list[float] = field(default_factory=list)
    plate_widths: list[float] = field(default_factory=list)
    plate_heights: list[float] = field(default_factory=list)
    aspect_ratios: list[float] = field(default_factory=list)
    frame_fractions: list[float] = field(default_factory=list)
    vehicle_fractions: list[float] = field(default_factory=list)
    resolutions: Counter = field(default_factory=Counter)
    tracks: set = field(default_factory=set)
    sources: Counter = field(default_factory=Counter)
    estimated_glyphs: int = 0

    @property
    def total(self) -> int:
        return sum(self.counts.values())

    @property
    def unique_vehicles(self) -> int:
        return len({t for t in self.tracks if t})

    def percentages(self) -> dict[str, float]:
        total = self.total or 1
        return {
            name: round(100 * self.counts.get(name, 0) / total, 2)
            for name, _, _ in DEFAULT_SIZE_BUCKETS
        }

    def glyph_percentiles(self) -> dict[str, float]:
        return percentile_summary(self.glyph_values)


def aggregate(observations: list[PlateObservation], label: str = "all") -> SizeDistribution:
    distribution = SizeDistribution(label=label)
    for observation in observations:
        distribution.counts[observation.bucket] += 1
        distribution.glyph_values.append(observation.glyph_px)
        distribution.plate_widths.append(observation.width)
        distribution.plate_heights.append(observation.height)
        distribution.aspect_ratios.append(observation.aspect_ratio)
        distribution.frame_fractions.append(observation.frame_area_fraction)
        vehicle_fraction = observation.vehicle_area_fraction
        if vehicle_fraction is not None:
            distribution.vehicle_fractions.append(vehicle_fraction)
        distribution.resolutions[f"{observation.frame_width}x{observation.frame_height}"] += 1
        distribution.tracks.add(observation.track_id)
        distribution.sources[observation.source] += 1
        if observation.is_estimated_glyph:
            distribution.estimated_glyphs += 1
    return distribution


def group_by(observations: list[PlateObservation], attribute: str) -> dict[str, SizeDistribution]:
    grouped: dict[str, list[PlateObservation]] = defaultdict(list)
    for observation in observations:
        grouped[str(getattr(observation, attribute, UNKNOWN) or UNKNOWN)].append(observation)
    return {key: aggregate(values, label=key) for key, values in sorted(grouped.items())}


# ---- model implications --------------------------------------------------

SCENARIO_A = "A"
SCENARIO_B = "B"
SCENARIO_C = "C"


@dataclass
class ModelImplication:
    scenario: str
    headline: str
    detail: str
    under_20: float
    under_30: float
    band_20_30: float
    band_30_50: float
    over_50: float


def model_implication(distribution: SizeDistribution) -> ModelImplication:
    """Translate a measured distribution into the pre-agreed engineering call.

    The thresholds and the three scenarios were fixed in the M1.5 brief BEFORE
    any measurement existed, which is the point: the decision rule is not chosen
    after seeing the numbers.
    """
    percentages = distribution.percentages()
    under_20 = percentages.get("<20px", 0.0)
    band_20_30 = percentages.get("20-30px", 0.0)
    under_30 = round(under_20 + band_20_30, 2)
    band_30_50 = percentages.get("30-50px", 0.0)
    over_50 = round(
        percentages.get("50-75px", 0.0) + percentages.get("75-100px", 0.0)
        + percentages.get(">100px", 0.0), 2,
    )

    if under_20 > 50.0:
        scenario, headline = SCENARIO_C, "Capture-side work first — most plates are below 20px"
        detail = (
            "A majority of plates carry fewer pixels than the recogniser's input height. No "
            "architecture recovers information that was never captured, and claiming otherwise "
            "would be the exact failure this project's honesty rule exists to prevent. "
            "Recommend camera placement, focal length, resolution and shutter-speed changes "
            "BEFORE any model training, and implement an explicit INSUFFICIENT_RESOLUTION "
            "state so the system declines rather than emitting a confident guess."
        )
    elif over_50 >= 50.0:
        scenario, headline = SCENARIO_A, "Proceed as planned — most plates exceed 50px"
        detail = (
            "The planned CRNN + CTC primary with PARSeq as the ceiling comparator is "
            "appropriate at these sizes, and the 32px input height is comfortable. Proceed to "
            "M2 once authorized data exists."
        )
    else:
        scenario, headline = SCENARIO_B, "Small-plate regime — 20-50px dominates"
        detail = (
            "Most plates sit between 20 and 50px, which is readable but marginal. Before "
            "committing to an architecture, investigate: recogniser input resolution (32px may "
            "be too small a target height); frame selection, picking the largest observation per "
            "track rather than the first; and camera configuration. Super-resolution only if it "
            "is empirically justified on a real test set — never on the assumption that it helps."
        )
    return ModelImplication(
        scenario=scenario, headline=headline, detail=detail,
        under_20=under_20, under_30=under_30, band_20_30=band_20_30,
        band_30_50=band_30_50, over_50=over_50,
    )


# ---- reporting -----------------------------------------------------------

def _bucket_table(distribution: SizeDistribution) -> list[str]:
    percentages = distribution.percentages()
    lines = ["| bucket | plates | % |", "|---|---|---|"]
    for name, _, _ in DEFAULT_SIZE_BUCKETS:
        lines.append(f"| {name} | {distribution.counts.get(name, 0)} | {percentages[name]} |")
    return lines


def render_report(
    observations: list[PlateObservation],
    max_per_track: int = 3,
    sampling_note: str = "",
) -> str:
    """The full M1.5 report: provenance, both weightings, per-camera and
    per-condition breakdowns, and the resulting engineering recommendation."""
    if not observations:
        return (
            "# CCTV plate-size analysis\n\n"
            "**No observations.** No footage was supplied, or no plate regions were found.\n\n"
            "No statistics are reported, because none were measured.\n"
        )

    frame_weighted = aggregate(observations, "frame-weighted")
    weighted_observations = vehicle_weighted(observations, max_per_track)
    per_vehicle = aggregate(weighted_observations, "vehicle-weighted")

    sources = frame_weighted.sources
    ground_truth_only = set(sources) == {SOURCE_GROUND_TRUTH}
    provenance = (
        "human-annotated ground-truth boxes" if ground_truth_only
        else "**DETECTOR-ESTIMATED** plate boxes"
    )

    lines = [
        "# CCTV plate-size analysis",
        "",
        f"**Box provenance: {provenance}**",
        "",
    ]
    if not ground_truth_only:
        lines += [
            "> These sizes come from the plate detector, **not** from human annotation. On the "
            "labelled benchmark that detector reached mean IoU 0.136, with 12 of 16 detections "
            "landing on something that was not a plate. Treat every number below as a "
            "*detector-estimated* distribution, not as ground truth. Calibrate against a "
            "manually boxed sample before relying on it for an architecture decision.",
            "",
        ]
    if frame_weighted.estimated_glyphs:
        lines += [
            f"> Glyph height is ESTIMATED from box height for "
            f"{frame_weighted.estimated_glyphs} of {frame_weighted.total} observations "
            "(55% of box height for single-row plates, 27.5% for two-row). A real "
            "character-band measurement always overrides the estimate where one exists.",
            "",
        ]
    if sampling_note:
        lines += [f"Sampling: {sampling_note}", ""]

    lines += [
        "## Sampling weight comparison",
        "",
        "A vehicle stationary for 200 frames would contribute 200 observations and describe "
        "itself rather than the traffic. Both weightings are shown so that bias is visible.",
        "",
        f"- frame-weighted: **{frame_weighted.total}** observations, "
        f"{frame_weighted.unique_vehicles} unique vehicles/tracks",
        f"- vehicle-weighted (max {max_per_track} per track): **{per_vehicle.total}** "
        f"observations, {per_vehicle.unique_vehicles} unique vehicles/tracks",
        "",
        "| bucket | frame-weighted % | vehicle-weighted % | difference |",
        "|---|---|---|---|",
    ]
    frame_pct, vehicle_pct = frame_weighted.percentages(), per_vehicle.percentages()
    largest_shift = 0.0
    for name, _, _ in DEFAULT_SIZE_BUCKETS:
        shift = round(vehicle_pct[name] - frame_pct[name], 2)
        largest_shift = max(largest_shift, abs(shift))
        lines.append(f"| {name} | {frame_pct[name]} | {vehicle_pct[name]} | {shift:+.2f} |")
    lines += [
        "",
        f"Largest shift between weightings: **{largest_shift:.2f} pp**. "
        + ("A shift this size means frame-weighted figures are materially biased by "
           "long-dwelling vehicles; use the vehicle-weighted column."
           if largest_shift >= 5.0 else
           "The two weightings agree closely, so dwell-time bias is not distorting this sample."),
        "",
        "**All figures below are vehicle-weighted.**",
        "",
        "## Overall distribution",
        "",
    ]
    lines += _bucket_table(per_vehicle)

    glyph = per_vehicle.glyph_percentiles()
    lines += [
        "",
        "### Estimated glyph height (px)",
        "",
        "| n | min | p10 | p25 | p50 | p75 | p90 | p95 | max | mean |",
        "|---|---|---|---|---|---|---|---|---|---|",
        f"| {glyph['n']} | {glyph['min']} | {glyph['p10']} | {glyph['p25']} | {glyph['p50']} "
        f"| {glyph['p75']} | {glyph['p90']} | {glyph['p95']} | {glyph['max']} | {glyph['mean']} |",
        "",
        "### Plate box dimensions (px)",
        "",
        "| measure | p10 | p50 | p90 |",
        "|---|---|---|---|",
    ]
    for name, values in (
        ("width", per_vehicle.plate_widths),
        ("height", per_vehicle.plate_heights),
        ("aspect ratio", per_vehicle.aspect_ratios),
    ):
        summary = percentile_summary(values)
        lines.append(f"| {name} | {summary['p10']} | {summary['p50']} | {summary['p90']} |")

    frame_fraction = percentile_summary(per_vehicle.frame_fractions)
    lines += [
        "",
        f"Plate area as a fraction of the frame: p50 **{frame_fraction['p50']:.5f}**, "
        f"p90 {frame_fraction['p90']:.5f}",
    ]
    if per_vehicle.vehicle_fractions:
        vehicle_fraction = percentile_summary(per_vehicle.vehicle_fractions)
        lines.append(
            f"Plate area as a fraction of its vehicle crop: p50 "
            f"**{vehicle_fraction['p50']:.4f}**, p90 {vehicle_fraction['p90']:.4f}"
        )
    else:
        lines.append("Plate-to-vehicle area fraction: not measured (no vehicle boxes supplied).")

    # --- breakdowns ---
    for attribute, title in (
        ("camera_id", "Per camera"),
        ("time_of_day", "Day / night"),
        ("vehicle_category", "Vehicle type"),
        ("plate_face", "Front / rear"),
        ("row_layout", "Plate layout (single vs two-row)"),
    ):
        groups = group_by(weighted_observations, attribute)
        if len(groups) <= 1 and UNKNOWN in groups:
            lines += ["", f"## {title}", "", "Not reported — the footage carried no such metadata."]
            continue
        lines += ["", f"## {title}", "",
                  "| group | plates | vehicles | resolution | " +
                  " | ".join(b[0] for b in DEFAULT_SIZE_BUCKETS) + " | p50 glyph |",
                  "|---" * (5 + len(DEFAULT_SIZE_BUCKETS)) + "|"]
        for key, group in groups.items():
            percentages = group.percentages()
            resolution = group.resolutions.most_common(1)[0][0] if group.resolutions else UNKNOWN
            lines.append(
                f"| {key} | {group.total} | {group.unique_vehicles} | {resolution} | "
                + " | ".join(f"{percentages[b[0]]}" for b in DEFAULT_SIZE_BUCKETS)
                + f" | {group.glyph_percentiles()['p50']} |"
            )

    # --- implications ---
    implication = model_implication(per_vehicle)
    lines += [
        "",
        "## Model-selection implications",
        "",
        "| band | % of plates |",
        "|---|---|",
        f"| <20px | {implication.under_20} |",
        f"| 20-30px | {implication.band_20_30} |",
        f"| **<30px total** | **{implication.under_30}** |",
        f"| 30-50px | {implication.band_30_50} |",
        f"| >50px | {implication.over_50} |",
        "",
        f"### Scenario {implication.scenario} — {implication.headline}",
        "",
        implication.detail,
        "",
        "> Plates below 20px are retained in this report, never discarded. They are the "
        "operational case for an explicit `INSUFFICIENT_RESOLUTION` state: a system that "
        "cannot read a plate should say so rather than emit a confident guess.",
    ]
    return "\n".join(lines) + "\n"
