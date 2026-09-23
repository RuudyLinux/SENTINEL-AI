"""Recognition metrics, calibration, and paired model comparison.

Definitions are pinned deliberately, because the whole Phase 3 comparison rests
on before/after numbers being computed the same way. `tests/test_metrics.py`
asserts this module's edit distance against the backend benchmark's
implementation (`backend/tools/anpr_bench.py::levenshtein`), so the two cannot
drift apart unnoticed — two definitions of CER is how a before/after comparison
quietly stops being a comparison.

Nothing here fabricates a result. Calibration returns empty structures until
real predictions with real confidences exist; McNemar refuses to run unless both
systems were scored on exactly the same examples.

Stdlib only.
"""
from __future__ import annotations

import math
from collections import Counter
from dataclasses import dataclass, field


# ---- edit distance ------------------------------------------------------

def levenshtein(a: str, b: str) -> int:
    """Edit distance. Same algorithm and same result as the backend benchmark's
    implementation, which is what produced the 0.3983 CER baseline."""
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


def edit_operations(truth: str, prediction: str) -> list[tuple[str, str, str]]:
    """Backtrace the alignment into `(op, truth_char, predicted_char)`.

    Phase 2 showed the MIX matters, not just the total: 25 substitutions but
    also 20 deletions and 12 insertions, which together say the recogniser is
    failing at segmentation rather than at glyph identity. A single CER number
    hides that distinction entirely, so the operations are always available.
    """
    rows, cols = len(truth) + 1, len(prediction) + 1
    dist = [[0] * cols for _ in range(rows)]
    for i in range(rows):
        dist[i][0] = i
    for j in range(cols):
        dist[0][j] = j
    for i in range(1, rows):
        for j in range(1, cols):
            cost = 0 if truth[i - 1] == prediction[j - 1] else 1
            dist[i][j] = min(dist[i - 1][j] + 1, dist[i][j - 1] + 1, dist[i - 1][j - 1] + cost)

    operations: list[tuple[str, str, str]] = []
    i, j = len(truth), len(prediction)
    while i > 0 or j > 0:
        if (i > 0 and j > 0
                and dist[i][j] == dist[i - 1][j - 1] + (0 if truth[i - 1] == prediction[j - 1] else 1)):
            if truth[i - 1] != prediction[j - 1]:
                operations.append(("sub", truth[i - 1], prediction[j - 1]))
            i, j = i - 1, j - 1
        elif i > 0 and dist[i][j] == dist[i - 1][j] + 1:
            operations.append(("del", truth[i - 1], ""))
            i -= 1
        else:
            operations.append(("ins", "", prediction[j - 1]))
            j -= 1
    return list(reversed(operations))


# ---- aggregate recognition metrics --------------------------------------

@dataclass
class RecognitionMetrics:
    total: int = 0
    exact: int = 0
    substitutions: int = 0
    insertions: int = 0
    deletions: int = 0
    truth_characters: int = 0
    confusions: Counter = field(default_factory=Counter)

    @property
    def total_edits(self) -> int:
        return self.substitutions + self.insertions + self.deletions

    @property
    def exact_match(self) -> float:
        return self.exact / self.total if self.total else 0.0

    @property
    def cer(self) -> float:
        """Character error rate: total edits / ground-truth characters.

        Denominator is the TRUTH length, so an over-long prediction can push CER
        above 1.0. That is intended — a read that invents ten characters is
        worse than one that reads nothing, and a metric capped at 1.0 would hide
        it.
        """
        return self.total_edits / self.truth_characters if self.truth_characters else 0.0

    @property
    def character_accuracy(self) -> float:
        """1 - CER, floored at 0. Stated explicitly so it is never confused with
        exact-match accuracy, which is a per-PLATE measure."""
        return max(0.0, 1.0 - self.cer)

    def as_dict(self) -> dict:
        return {
            "samples": self.total,
            "exact_match": round(self.exact_match, 4),
            "character_accuracy": round(self.character_accuracy, 4),
            "cer": round(self.cer, 4),
            "substitutions": self.substitutions,
            "insertions": self.insertions,
            "deletions": self.deletions,
            "total_edits": self.total_edits,
            "truth_characters": self.truth_characters,
        }


def normalize(text: str) -> str:
    """Case-normalized comparison form. Nothing else — no character repair, no
    format coercion. The evaluator must measure what the model produced."""
    return (text or "").strip().upper()


def evaluate(pairs: list[tuple[str, str]]) -> RecognitionMetrics:
    """Score `(ground_truth, prediction)` pairs."""
    metrics = RecognitionMetrics()
    for raw_truth, raw_prediction in pairs:
        truth, prediction = normalize(raw_truth), normalize(raw_prediction)
        metrics.total += 1
        metrics.truth_characters += len(truth)
        if truth == prediction:
            metrics.exact += 1
        for operation, truth_char, predicted_char in edit_operations(truth, prediction):
            if operation == "sub":
                metrics.substitutions += 1
                metrics.confusions[(truth_char, predicted_char)] += 1
            elif operation == "ins":
                metrics.insertions += 1
            else:
                metrics.deletions += 1
    return metrics


# ---- confidence intervals ----------------------------------------------

def wilson_interval(successes: int, total: int, z: float = 1.96) -> tuple[float, float]:
    """95% Wilson score interval for a proportion.

    Used rather than the normal approximation because it behaves correctly at
    small n and near 0/1 — precisely the regime a 500-plate test set sits in.
    """
    if total <= 0:
        return (0.0, 0.0)
    p = successes / total
    denominator = 1 + z * z / total
    centre = (p + z * z / (2 * total)) / denominator
    spread = z * math.sqrt(p * (1 - p) / total + z * z / (4 * total * total)) / denominator
    return (max(0.0, centre - spread), min(1.0, centre + spread))


# ---- paired model comparison -------------------------------------------

@dataclass
class PairedComparison:
    n: int
    both_correct: int
    only_a: int
    only_b: int
    both_wrong: int
    statistic: float
    p_value: float

    @property
    def significant(self) -> bool:
        return self.p_value < 0.05

    def as_dict(self) -> dict:
        return {
            "n": self.n, "both_correct": self.both_correct,
            "only_a_correct": self.only_a, "only_b_correct": self.only_b,
            "both_wrong": self.both_wrong,
            "mcnemar_statistic": round(self.statistic, 4),
            "p_value": round(self.p_value, 6),
            "significant_at_0.05": self.significant,
        }


def _chi2_sf_1df(x: float) -> float:
    """Survival function of chi-squared with 1 degree of freedom.

    For 1 df this is exactly `erfc(sqrt(x/2))`, so no scipy dependency is
    needed — which keeps this tooling stdlib-only and runnable anywhere.
    """
    return math.erfc(math.sqrt(x / 2.0)) if x > 0 else 1.0


def mcnemar(
    truths: dict[str, str],
    predictions_a: dict[str, str],
    predictions_b: dict[str, str],
) -> PairedComparison:
    """Paired significance test between two systems on the SAME examples.

    Keys are example ids. Raises if the two systems were not scored on an
    identical example set — comparing a model evaluated on 480 samples against
    one evaluated on 500 is not a paired test, and silently intersecting them
    would produce a number that looks valid and is not.

    Uses the exact binomial test on the discordant pairs when they are few
    (< 25), where the chi-squared approximation is unreliable; otherwise the
    continuity-corrected chi-squared statistic.
    """
    keys_a, keys_b = set(predictions_a), set(predictions_b)
    if keys_a != keys_b:
        missing = (keys_a ^ keys_b)
        raise ValueError(
            f"paired comparison requires identical example sets; {len(missing)} differ "
            f"(e.g. {sorted(missing)[:3]})"
        )
    missing_truth = keys_a - set(truths)
    if missing_truth:
        raise ValueError(f"no ground truth for {len(missing_truth)} example(s)")

    both_correct = only_a = only_b = both_wrong = 0
    for key in sorted(keys_a):
        truth = normalize(truths[key])
        a_ok = normalize(predictions_a[key]) == truth
        b_ok = normalize(predictions_b[key]) == truth
        if a_ok and b_ok:
            both_correct += 1
        elif a_ok:
            only_a += 1
        elif b_ok:
            only_b += 1
        else:
            both_wrong += 1

    discordant = only_a + only_b
    if discordant == 0:
        return PairedComparison(len(keys_a), both_correct, only_a, only_b, both_wrong, 0.0, 1.0)

    if discordant < 25:
        # Exact two-sided binomial test at p=0.5 on the discordant pairs.
        smaller = min(only_a, only_b)
        tail = sum(math.comb(discordant, k) for k in range(smaller + 1)) / (2 ** discordant)
        p_value = min(1.0, 2 * tail)
        statistic = float(smaller)
    else:
        statistic = (abs(only_a - only_b) - 1) ** 2 / discordant
        p_value = _chi2_sf_1df(statistic)
    return PairedComparison(
        len(keys_a), both_correct, only_a, only_b, both_wrong, statistic, p_value,
    )


# ---- confidence calibration --------------------------------------------

@dataclass
class CalibrationBin:
    lower: float
    upper: float
    count: int
    mean_confidence: float
    accuracy: float

    def as_dict(self) -> dict:
        return {
            "range": f"[{self.lower:.1f}, {self.upper:.1f})",
            "count": self.count,
            "mean_confidence": round(self.mean_confidence, 4),
            "accuracy": round(self.accuracy, 4),
            "gap": round(self.mean_confidence - self.accuracy, 4),
        }


@dataclass
class CalibrationReport:
    bins: list[CalibrationBin] = field(default_factory=list)
    expected_calibration_error: float = 0.0
    samples: int = 0

    def as_dict(self) -> dict:
        return {
            "samples": self.samples,
            "expected_calibration_error": round(self.expected_calibration_error, 4),
            "bins": [b.as_dict() for b in self.bins],
        }


def calibration(
    outcomes: list[tuple[float, bool]], bin_count: int = 10,
) -> CalibrationReport:
    """Reliability bins and expected calibration error.

    `outcomes` is `(confidence, was_correct)` per prediction.

    This matters more here than in a typical model report, because the
    production pipeline CONSUMES confidence: `plate_min_confidence` gates
    persistence and `plate_review_confidence_floor` decides whether a human ever
    sees a read. A recogniser reporting 0.9 on reads that are right 60% of the
    time would silently defeat both gates, however good its headline accuracy.

    Returns an empty report for empty input — it never invents bins, and no
    calibration number exists until real predictions do.
    """
    report = CalibrationReport(samples=len(outcomes))
    if not outcomes:
        return report

    width = 1.0 / bin_count
    for index in range(bin_count):
        lower, upper = index * width, (index + 1) * width
        # Last bin is closed so a confidence of exactly 1.0 is counted.
        members = [
            (c, ok) for c, ok in outcomes
            if (lower <= c < upper) or (index == bin_count - 1 and c == 1.0)
        ]
        if not members:
            continue
        mean_confidence = sum(c for c, _ in members) / len(members)
        accuracy = sum(1 for _, ok in members if ok) / len(members)
        report.bins.append(CalibrationBin(lower, upper, len(members), mean_confidence, accuracy))

    report.expected_calibration_error = sum(
        (b.count / len(outcomes)) * abs(b.mean_confidence - b.accuracy) for b in report.bins
    )
    return report
