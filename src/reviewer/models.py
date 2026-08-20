"""Pydantic contracts for every stage of the review pipeline.

The LLM-facing models (``LLMFileChangeReview`` and everything it contains) are
serialised into a DeepSeek *strict* function schema, so they must stay free of
constructs the strict validator rejects: no bare ``dict``, no unions other than
``Optional``, no default-only fields that we would want omitted.
"""

from __future__ import annotations

from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


# --------------------------------------------------------------------------
# Enums shared by the reviewer output schema
# --------------------------------------------------------------------------


class Severity(str, Enum):
    ERROR = "error"
    WARNING = "warning"
    INFO = "info"


class Category(str, Enum):
    SECURITY = "security"
    PERFORMANCE = "performance"
    STYLE = "style"
    LOGIC = "logic"
    MAINTAINABILITY = "maintainability"
    TESTING = "testing"


class LineState(str, Enum):
    """Where a cited line lives relative to the diff.

    ``null`` is deliberately absent: a comment that cannot name which side of
    the diff it sits on cannot be scope-checked, and an un-scope-checkable
    citation is exactly the bypass this pipeline exists to prevent.
    """

    DIFF_ADDED = "diff-added"
    DIFF_REMOVED = "diff-removed"
    FILE_CONTEXT = "file-context"


class OverallRating(str, Enum):
    EXCELLENT = "excellent"
    GOOD = "good"
    NEEDS_IMPROVEMENT = "needs_improvement"
    POOR = "poor"


class Complexity(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class TestCoverage(str, Enum):
    GOOD = "good"
    ADEQUATE = "adequate"
    INSUFFICIENT = "insufficient"
    NONE = "none"


class Maintainability(str, Enum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


# --------------------------------------------------------------------------
# Reviewer output schema (LLM-facing)
# --------------------------------------------------------------------------


class LineNumber(BaseModel):
    line_number: int
    line_number_state: LineState


class Metrics(BaseModel):
    complexity: Complexity
    test_coverage: TestCoverage
    maintainability: Maintainability


class ReviewComment(BaseModel):
    line_numbers: list[LineNumber]
    severity: Severity
    category: Category
    message: str
    criteria: str
    suggestion: str
    rule: str
    implementation_complexity: Complexity
    context_needed: bool
    implementation_notes: Optional[str] = None


class LLMFileChangeReview(BaseModel):
    overall_rating: OverallRating
    summary: str
    comments: list[ReviewComment]
    metrics: Metrics


# --------------------------------------------------------------------------
# Pipeline-internal models (never sent to the LLM as a schema)
# --------------------------------------------------------------------------


class Hunk(BaseModel):
    """One ``@@`` section of a unified diff."""

    header: str
    old_start: int
    new_start: int
    lines: list[tuple[str, Optional[int], Optional[int], str]] = Field(
        default_factory=list,
        description="(marker, old_lineno, new_lineno, content) per raw diff line",
    )


class FileChange(BaseModel):
    filepath: str
    old_path: Optional[str] = None
    status: str = "modified"  # added | modified | deleted | renamed
    hunks: list[Hunk] = Field(default_factory=list)
    raw_patch: str = ""

    @property
    def added_lines(self) -> set[int]:
        out: set[int] = set()
        for hunk in self.hunks:
            for marker, _old, new, _content in hunk.lines:
                if marker == "+" and new is not None:
                    out.add(new)
        return out

    @property
    def removed_lines(self) -> set[int]:
        out: set[int] = set()
        for hunk in self.hunks:
            for marker, old, _new, _content in hunk.lines:
                if marker == "-" and old is not None:
                    out.add(old)
        return out


class CodeChangeInfo(BaseModel):
    """Everything the reviewer knows about the change under review."""

    repository: str
    cc_id: str
    cc_title: str
    cc_description: str
    source_branch: str
    target_branch: str
    # The commit this review is *about*. Dedup keys on it, so a review is
    # answered by the exact revision it examined rather than by a clock.
    head_sha: str = ""
    changes: list[FileChange] = Field(default_factory=list)


class FileChangeReviewRequest(BaseModel):
    fcid: str
    filepath: str
    policies: list[str] = Field(default_factory=list)
    rendered_diff: str = ""
    chunk_index: int = 0
    chunk_total: int = 1
    general_notes: str = ""


class FileChangeReview(BaseModel):
    """Post-validation, per-file result."""

    filepath: str
    diff: str = ""
    ai_comments: list[ReviewComment] = Field(default_factory=list)
    out_of_scope_comments: list[ReviewComment] = Field(default_factory=list)
    overall_rating: OverallRating = OverallRating.GOOD
    summary: str = ""
    metrics: Optional[Metrics] = None
    degraded: bool = False


class PullRequestReview(BaseModel):
    cc_id: str
    head_sha: str = ""
    file_reviews: list[FileChangeReview] = Field(default_factory=list)
    overall_rating: OverallRating = OverallRating.GOOD
    summary: str = ""
    total_files: int = 0
    skipped_files: int = 0

    @property
    def total_comments(self) -> int:
        return sum(len(r.ai_comments) for r in self.file_reviews)

    @property
    def error_count(self) -> int:
        return sum(
            1
            for r in self.file_reviews
            for c in r.ai_comments
            if c.severity is Severity.ERROR
        )

    @property
    def warning_count(self) -> int:
        return sum(
            1
            for r in self.file_reviews
            for c in r.ai_comments
            if c.severity is Severity.WARNING
        )


# --------------------------------------------------------------------------
# Validation pipeline verdicts
# --------------------------------------------------------------------------


class QualifyVerdict(str, Enum):
    DISCARD = "discard"
    VALIDATE = "validate"
    PASS = "pass"


class ValuableDecision(BaseModel):
    """Would a maintainer act on this finding?

    Applied to info-severity comments, which are too cheap to justify an
    agentic validation pass but too numerous to retain unconditionally.
    Reasoning first so the model works the question before committing.
    """

    reasoning: str
    valuable: bool


class ValidateVerdict(str, Enum):
    TRUE_POSITIVE = "TRUE_POSITIVE"
    FALSE_POSITIVE = "FALSE_POSITIVE"
    FALSE_POSITIVE_OOS = "FALSE_POSITIVE_OOS"
    TRUE_POSITIVE_SEVERITY_INFO = "TRUE_POSITIVE_SEVERITY_INFO"
    INDETERMINATE = "INDETERMINATE"
