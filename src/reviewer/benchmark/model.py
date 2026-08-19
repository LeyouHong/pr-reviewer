"""Benchmark data contracts.

The corpus is the only thing that makes prompt changes measurable rather than
guessed at, so its shape is deliberately boring and hand-editable: one JSON
file, ground truth written by a person.
"""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field

from ..models import ValuableDecision  # re-exported for benchmark callers


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


class PrPart(BaseModel):
    """One PR's outcome, checkpointed to disk so a crash costs one PR."""

    pr_id: str
    matcher_version: str
    findings: list[ScoredFinding] = Field(default_factory=list)
    ground_truth: list[GroundTruthIssue] = Field(default_factory=list)
    failed: bool = False
    error: str = ""


class MatchDecision(BaseModel):
    """LLM-facing: reasoning first so the procedure runs before the verdict."""

    reasoning: str
    matched_gt_id: Optional[str] = None


class Score(BaseModel):
    true_positives: int = 0
    false_positives: int = 0
    false_negatives: int = 0

    @property
    def precision(self) -> float:
        denom = self.true_positives + self.false_positives
        return self.true_positives / denom if denom else 0.0

    @property
    def recall(self) -> float:
        denom = self.true_positives + self.false_negatives
        return self.true_positives / denom if denom else 0.0

    @property
    def f1(self) -> float:
        p, r = self.precision, self.recall
        return 2 * p * r / (p + r) if (p + r) else 0.0

__all__ = [n for n in dir() if not n.startswith('_')]
