"""Benchmark data contracts.

The corpus is the only thing that makes prompt changes measurable rather than
guessed at, so its shape is deliberately boring and hand-editable: one JSON
file, ground truth written by a person.
"""

from __future__ import annotations

from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field

from ..models import ValuableDecision  # re-exported for benchmark callers


class JudgeVerdict(str, Enum):
    """Three-way match outcome, so PARTIAL is a first-class signal.

    A binary matcher collapses "same construct, wrong root cause" into
    NO_MATCH, which then reads as a false positive in the scorecard even
    though the reviewer surfaced the right code site. Naming PARTIAL
    separately lets prompt work target that failure mode specifically.
    """

    MATCH = "MATCH"
    PARTIAL = "PARTIAL"
    NO_MATCH = "NO_MATCH"


class GroundTruthIssue(BaseModel):
    """One root cause at one code site that a maintainer fixes with one commit.

    Granularity rule: two dereferences of the same nullable value are one
    issue; the same bug pattern at two sites in different methods are two.
    """

    id: str
    file: str
    lines: list[int] = Field(default_factory=list)
    description: str
    # Lowest severity at which a reviewer finding still counts as catching this.
    min_severity: str = "error"
    # Value tier, orthogonal to severity: p1 production-critical, p2 real but
    # narrow, p3 cosmetic / test-only / latent with no callers.
    value: Optional[str] = None
    # True when confirming the defect needs files outside the diff. Splits
    # recall into diff-only and cross-file, which fail for different reasons.
    requires_exploration: bool = False
    # Free-text audit trail: why a label was amended, downgraded, or rejected.
    note: str = ""


class CorpusPr(BaseModel):
    id: str
    repo: str
    number: int
    title: str = ""
    description: str = ""
    base_branch: str = "main"
    head_branch: str = ""
    # Labels are anchored to this commit. Later commits do not update them.
    pin_commit: str = ""
    diff: str = ""
    ground_truth: list[GroundTruthIssue] = Field(default_factory=list)
    labelled: bool = False


class Corpus(BaseModel):
    version: int = 1
    prs: list[CorpusPr] = Field(default_factory=list)


class RawFinding(BaseModel):
    file: str
    lines: list[int] = Field(default_factory=list)
    severity: str
    category: str
    message: str
    suggestion: str = ""


class ScoredFinding(RawFinding):
    matched_gt_id: Optional[str] = None
    reasoning: str = ""
    # ``verdict`` is the raw 3-class judge outcome. ``matched_gt_id`` is the
    # binary collapse used by ``scoring.py`` (only MATCH counts as recall).
    # Keeping both lets the report characterise PARTIAL separately without
    # rebreaking the historical scorecard.
    verdict: JudgeVerdict = JudgeVerdict.NO_MATCH


class PrPart(BaseModel):
    """One PR's outcome, checkpointed to disk so a crash costs one PR."""

    pr_id: str
    matcher_version: str
    findings: list[ScoredFinding] = Field(default_factory=list)
    ground_truth: list[GroundTruthIssue] = Field(default_factory=list)
    failed: bool = False
    error: str = ""


class JudgeDecision(BaseModel):
    """LLM-facing 3-class judge output. Reasoning before the verdict.

    ``matched_gt_id`` is the ground-truth id the candidate points at; carried
    for both MATCH and PARTIAL so PARTIAL entries are still comparable across
    passes. A NO_MATCH must carry a null id.
    """

    reasoning: str
    verdict: JudgeVerdict
    matched_gt_id: Optional[str] = None


class Score(BaseModel):
    """Precision counts findings; recall counts known bugs.

    The two are deliberately separate counters. Recall was once computed as
    ``true_positives / (true_positives + missed)``, which puts findings in the
    numerator and a mix of findings and bugs in the denominator: reporting one
    real bug twice inflated it. That made a run that merely stopped duplicating
    itself look like a regression.
    """

    # Finding-side, for precision.
    true_positives: int = 0
    false_positives: int = 0
    # Bug-side, for recall. A bug counts once no matter how often it is found.
    recalled: int = 0
    ground_truth: int = 0

    @property
    def precision(self) -> float:
        denom = self.true_positives + self.false_positives
        return self.true_positives / denom if denom else 0.0

    @property
    def recall(self) -> float:
        return self.recalled / self.ground_truth if self.ground_truth else 0.0

    @property
    def f1(self) -> float:
        p, r = self.precision, self.recall
        return 2 * p * r / (p + r) if (p + r) else 0.0

__all__ = [n for n in dir() if not n.startswith('_')]
