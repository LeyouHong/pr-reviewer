"""Matcher calibration.

The matcher is itself an LLM, so it regresses when the model changes. These
hand-written (finding, ground truth, expected verdict) triples are the unit
test for the judge: run them before trusting a scorecard produced by a new
model, and never pool scores across matcher versions.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

from ..config import Config
from ..prompt import PromptLibrary
from ..provider import DeepSeekClient
from .matcher import MATCHER_VERSION, Matcher
from .model import CorpusPr, GroundTruthIssue, RawFinding

log = logging.getLogger(__name__)


@dataclass
class CalibrationCase:
    label: str
    finding: RawFinding
    ground_truth: list[GroundTruthIssue]
    diff_snippet: str
    expected_gt_id: Optional[str]


@dataclass
class CalibrationResult:
    label: str
    expected: Optional[str]
    actual: Optional[str]
    reasoning: str

    @property
    def passed(self) -> bool:
        return self.expected == self.actual


def run_calibration(
    config: Config, cases: list[CalibrationCase]
) -> tuple[list[CalibrationResult], str]:
    client = DeepSeekClient(config)
    matcher = Matcher(client, PromptLibrary(config.resources_dir))

    results: list[CalibrationResult] = []
    for case in cases:
        pr = CorpusPr(
            id=f"calibration:{case.label}",
            repo="calibration",
            number=0,
            ground_truth=case.ground_truth,
        )
        scored = matcher.match(case.finding, pr, case.diff_snippet)
        results.append(
            CalibrationResult(
                label=case.label,
                expected=case.expected_gt_id,
                actual=scored.matched_gt_id,
                reasoning=scored.reasoning,
            )
        )

    return results, format_calibration(results)


def format_calibration(results: list[CalibrationResult]) -> str:
    passed = sum(1 for r in results if r.passed)
    lines = [
        "# Matcher calibration",
        "",
        f"- matcher version: `{MATCHER_VERSION}`",
        f"- passed: {passed}/{len(results)}",
        "",
        "| case | expected | actual | result |",
        "|---|---|---|---|",
    ]
    for r in results:
        lines.append(
            f"| {r.label} | `{r.expected}` | `{r.actual}` | "
            f"{'pass' if r.passed else '**FAIL**'} |"
        )
    failures = [r for r in results if not r.passed]
    if failures:
        lines += ["", "## Failures", ""]
        for r in failures:
            lines += [f"### {r.label}", "", r.reasoning, ""]
    return "\n".join(lines) + "\n"
