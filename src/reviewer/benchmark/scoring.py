"""Turn checkpointed parts into precision / recall / F1.

Global F1 is the headline; the buckets below are the actionable signal. Recall
split by ``requires_exploration`` is the most useful one: if diff-only recall
is high and cross-file recall is low, the base reviewer is fine and the
validator's tool usage is what needs work.
"""

from __future__ import annotations

from collections import defaultdict

from .model import PrPart, Score


class Recall:
    """Recall over a subset of ground truth."""

    def __init__(self) -> None:
        self.found = 0
        self.total = 0

    @property
    def value(self) -> float:
        return self.found / self.total if self.total else 0.0


def score_parts(
    parts: list[PrPart],
) -> tuple[Score, dict[str, Score], dict[str, Recall], dict[str, Recall]]:
    """Return (overall, precision-by-severity, recall-by-difficulty, recall-by-value).

    A ground-truth bug counts as recalled if *any* finding matched it, so a
    reviewer is not rewarded for reporting the same bug three times.
    """
    versions = {p.matcher_version for p in parts if not p.failed}
    if len(versions) > 1:
        raise ValueError(f"cannot pool parts from different matchers: {versions}")

    overall = Score()
    by_severity: dict[str, Score] = defaultdict(Score)
    by_difficulty: dict[str, Recall] = {"diff-only": Recall(), "cross-file": Recall()}
    by_value: dict[str, Recall] = defaultdict(Recall)

    for part in parts:
        if part.failed:
            continue

        matched_ids: set[str] = set()
        for finding in part.findings:
            bucket = by_severity[finding.severity]
            if finding.matched_gt_id:
                matched_ids.add(finding.matched_gt_id)
                overall.true_positives += 1
                bucket.true_positives += 1
            else:
                overall.false_positives += 1
                bucket.false_positives += 1

        for issue in part.ground_truth:
            hit = issue.id in matched_ids
            if not hit:
                overall.false_negatives += 1
            difficulty = "cross-file" if issue.requires_exploration else "diff-only"
            by_difficulty[difficulty].total += 1
            by_difficulty[difficulty].found += int(hit)
            if issue.value:
                by_value[issue.value].total += 1
                by_value[issue.value].found += int(hit)

    return overall, dict(by_severity), by_difficulty, dict(by_value)


def format_report(
    overall: Score,
    by_severity: dict[str, Score],
    by_difficulty: dict[str, Recall],
    by_value: dict[str, Recall],
    parts: list[PrPart],
) -> str:
    failed = [p for p in parts if p.failed]
    clean = [p for p in parts if not p.failed and not p.ground_truth]

    lines = [
        "# Benchmark result",
        "",
        f"- PRs scored: {len(parts) - len(failed)}",
        f"- of which deliberately clean (zero known bugs): {len(clean)}",
        f"- PRs failed: {len(failed)}",
        "",
        "## Overall",
        "",
        "| metric | value |",
        "|---|---|",
        f"| precision | {overall.precision:.3f} |",
        f"| recall | {overall.recall:.3f} |",
        f"| F1 | {overall.f1:.3f} |",
        f"| true positives | {overall.true_positives} |",
        f"| false positives | {overall.false_positives} |",
        f"| missed bugs | {overall.false_negatives} |",
        "",
        "## Precision by reported severity",
        "",
        "| severity | precision | TP | FP |",
        "|---|---|---|---|",
    ]
    for severity in ("error", "warning", "info"):
        s = by_severity.get(severity)
        if not s:
            continue
        lines.append(
            f"| {severity} | {s.precision:.3f} | {s.true_positives} | {s.false_positives} |"
        )

    lines += [
        "",
        "## Recall by difficulty",
        "",
        "| bucket | recall | found | total |",
        "|---|---|---|---|",
    ]
    for name in ("diff-only", "cross-file"):
        r = by_difficulty[name]
        lines.append(f"| {name} | {r.value:.3f} | {r.found} | {r.total} |")

    if by_value:
        lines += [
            "",
            "## Recall by value tier",
            "",
            "| tier | recall | found | total |",
            "|---|---|---|---|",
        ]
        for tier in sorted(by_value):
            r = by_value[tier]
            lines.append(f"| {tier} | {r.value:.3f} | {r.found} | {r.total} |")

    if failed:
        lines += ["", "## Failed PRs", ""]
        lines += [f"- `{p.pr_id}`: {p.error}" for p in failed]
    return "\n".join(lines) + "\n"
