"""Split a review into a summary body + per-line inline comments.

Inline comments read better than one wall of markdown: the reader arrives at
the defect with the code right there. But mapping our finding schema onto
GitHub's review-comment payload has to be careful — a bad map is silent, the
API accepts a range with mismatched sides and shows nothing.

Anchor rule: the smallest cited line on the RIGHT (added) side is the anchor.
Comments that also cite REMOVED or FILE_CONTEXT lines still anchor on the
added line — the message body already lists every cited line, so the anchor
only needs to place the reader near the change. Comments citing *only*
removed lines anchor on LEFT.

Comments that cannot be mapped (no cited line on either side) are collected
and rendered into the review body verbatim, so nothing is lost silently.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from ..constants import REPORT_FINGERPRINT
from ..models import (
    LineState,
    OverallRating,
    PullRequestReview,
    ReviewComment,
    Severity,
)

_HEADING = {
    Severity.ERROR: "Errors",
    Severity.WARNING: "Warnings",
    Severity.INFO: "Information",
}


Side = Literal["RIGHT", "LEFT"]


@dataclass
class InlineComment:
    """The payload shape GitHub's review-comment API expects."""

    path: str
    line: int
    side: Side
    body: str


@dataclass
class InlineReview:
    """Everything the caller needs to post the review in one call."""

    body: str
    comments: list[InlineComment]
    unmapped: list[ReviewComment]


def _anchor(comment: ReviewComment) -> tuple[int, Side] | None:
    """Pick (line, side) for the inline anchor, or ``None`` if unmappable."""
    added = sorted(
        ln.line_number
        for ln in comment.line_numbers
        if ln.line_number_state is LineState.DIFF_ADDED
    )
    if added:
        return added[0], "RIGHT"
    removed = sorted(
        ln.line_number
        for ln in comment.line_numbers
        if ln.line_number_state is LineState.DIFF_REMOVED
    )
    if removed:
        return removed[0], "LEFT"
    return None


def _comment_body(comment: ReviewComment) -> str:
    cited = ", ".join(str(ln.line_number) for ln in comment.line_numbers)
    parts = [
        f"**{comment.severity.value}** — [{comment.criteria}] (Line {cited})",
        "",
        comment.message.strip(),
    ]
    if comment.suggestion:
        parts += ["", f"**Suggestion**: {comment.suggestion.strip()}"]
    if comment.implementation_notes:
        parts += ["", f"**Notes**: {comment.implementation_notes.strip()}"]
    return "\n".join(parts)


def _unmapped_block(filepath: str, comments: list[ReviewComment]) -> list[str]:
    """Fallback markdown for comments that could not anchor to a diff line."""
    if not comments:
        return []
    out = [f"### `{filepath}`", ""]
    for comment in sorted(
        comments, key=lambda c: min((ln.line_number for ln in c.line_numbers), default=0)
    ):
        cited = ", ".join(str(ln.line_number) for ln in comment.line_numbers) or "?"
        out += [
            f"- **{comment.severity.value}** [{comment.criteria}] "
            f"(cited lines: {cited})",
            f"  {comment.message.strip()}",
        ]
        if comment.suggestion:
            out.append(f"  - **Suggestion**: {comment.suggestion.strip()}")
    out.append("")
    return out


def build_inline_review(
    pr_review: PullRequestReview, commit: str = ""
) -> InlineReview:
    """Split ``pr_review`` into a summary body and per-line inline comments.

    The body carries the report fingerprint (so dedup still works) plus counts,
    the summary, and any comments that could not be mapped. Inline comments
    carry only the per-finding body — the reader already knows which file and
    line they're looking at.
    """
    inline: list[InlineComment] = []
    unmapped: dict[str, list[ReviewComment]] = {}

    for review in pr_review.file_reviews:
        for comment in review.ai_comments:
            anchor = _anchor(comment)
            if anchor is None:
                unmapped.setdefault(review.filepath, []).append(comment)
                continue
            line, side = anchor
            inline.append(
                InlineComment(
                    path=review.filepath,
                    line=line,
                    side=side,
                    body=_comment_body(comment),
                )
            )

    needs_work = sum(
        1
        for r in pr_review.file_reviews
        if r.overall_rating in (OverallRating.NEEDS_IMPROVEMENT, OverallRating.POOR)
    )

    body_lines = [REPORT_FINGERPRINT, "# Code review report", ""]
    if pr_review.summary:
        body_lines += [f"**Summary**: {pr_review.summary}", ""]
    body_lines += [
        f"**Overall rating**: {pr_review.overall_rating.value}",
        "",
        f"- Total files: {pr_review.total_files}",
        f"- Reviewed: {len(pr_review.file_reviews)}",
        f"- Skipped: {pr_review.skipped_files}",
        f"- Comments: {pr_review.total_comments}",
        f"- Errors: {pr_review.error_count}",
        f"- Files needing improvement: {needs_work}",
        "",
    ]
    if inline:
        body_lines += [
            f"See the {len(inline)} inline comment(s) attached to this review "
            "for per-line detail.",
            "",
        ]
    if unmapped:
        body_lines += [
            "## Findings without a diff anchor",
            "",
            "These findings could not be attached to a specific line "
            "(they cited context outside the diff) and are reported here in the "
            "review body instead.",
            "",
        ]
        for filepath, comments in sorted(unmapped.items()):
            body_lines += _unmapped_block(filepath, comments)

    body_lines += ["---", ""]
    diagnostics = [f"- Review id: {pr_review.cc_id}"]
    if commit:
        diagnostics.append(f"- Reviewer build: `{commit}`")
    body_lines += [
        "<details><summary>Diagnostics</summary>",
        "",
        *diagnostics,
        "",
        "</details>",
    ]

    flat_unmapped = [c for group in unmapped.values() for c in group]
    return InlineReview(
        body="\n".join(body_lines).rstrip() + "\n",
        comments=inline,
        unmapped=flat_unmapped,
    )


__all__ = ["InlineComment", "InlineReview", "build_inline_review"]
