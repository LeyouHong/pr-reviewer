"""Markdown rendering and the two summary rewrites."""

from __future__ import annotations

import json
import logging

from ..constants import REPORT_FINGERPRINT
from ..models import (
    CodeChangeInfo,
    OverallRating,
    FileChangeReview,
    PullRequestReview,
    ReviewComment,
    Severity,
)
from ..prompt import PromptLibrary, render
from ..provider import DeepSeekClient

log = logging.getLogger(__name__)

_HEADING = {
    Severity.ERROR: "Errors",
    Severity.WARNING: "Warnings",
    Severity.INFO: "Information",
}


def _issue_lines(comments: list[ReviewComment]) -> str:
    if not comments:
        return "(none)"
    out = []
    for c in comments:
        cited = ", ".join(f"L{ln.line_number}" for ln in c.line_numbers)
        out.append(f"- [{c.severity.value}] {cited}: {c.message}")
    return "\n".join(out)


class Summarizer:
    def __init__(self, client: DeepSeekClient, library: PromptLibrary):
        self._client = client
        self._library = library

    def patch_summary(
        self,
        review: FileChangeReview,
        removed: list[ReviewComment],
        info: CodeChangeInfo,
    ) -> str:
        """Rewrite a file summary that still references dropped findings."""
        if not removed:
            return review.summary
        if not review.ai_comments:
            return "No defects found in the reviewed code."

        prompt = render(
            self._library.task("patch_summary"),
            role_prompt=self._library.role("summarizer"),
            mr_project=info.repository,
            mr_title=info.cc_title,
            mr_source_branch=info.source_branch,
            mr_target_branch=info.target_branch,
            mr_description=info.cc_description or "(no description provided)",
            original_summary=review.summary or "(no summary)",
            removed_issues=_issue_lines(removed),
            retained_issues=_issue_lines(review.ai_comments),
        )
        try:
            return self._client.complete_text(
                prompt, label=f"patch_summary:{review.filepath}"
            ).strip()
        except Exception as exc:  # noqa: BLE001
            # Falling back to the original summary would leave the report
            # describing findings it no longer shows, so state only what
            # survived.
            log.warning("patch_summary failed for %s: %s", review.filepath, exc)
            count = len(review.ai_comments)
            return f"{count} finding(s) reported in `{review.filepath}`."

    def overall_summary(
        self, pr_review: PullRequestReview, info: CodeChangeInfo
    ) -> str:
        payload = json.dumps(
            [
                {
                    "filepath": r.filepath,
                    "summary": r.summary,
                    "comments": [
                        {"severity": c.severity.value, "message": c.message}
                        for c in r.ai_comments
                    ],
                }
                for r in pr_review.file_reviews
            ],
            indent=2,
            ensure_ascii=False,
        )
        prompt = render(
            self._library.task("summarize_v2"),
            role_prompt=self._library.role("summarizer"),
            mr_project=info.repository,
            mr_title=info.cc_title,
            mr_source_branch=info.source_branch,
            mr_target_branch=info.target_branch,
            mr_description=info.cc_description or "(no description provided)",
            input=payload,
        )
        try:
            return self._client.complete_text(prompt, label="summarize").strip()
        except Exception as exc:  # noqa: BLE001
            log.warning("overall summary failed: %s", exc)
            return "Review completed."


def to_markdown(pr_review: PullRequestReview, commit: str = "") -> str:
    """Render the final report posted to the pull request.

    Counts come before content deliberately: a reader who sees "3 errors across
    2 files" first arrives at the comments with calibrated expectations instead
    of meeting a wall of text.
    """
    needs_work = sum(
        1
        for r in pr_review.file_reviews
        if r.overall_rating
        in (OverallRating.NEEDS_IMPROVEMENT, OverallRating.POOR)
    )

    lines = [REPORT_FINGERPRINT, "# Code review report", ""]
    if pr_review.summary:
        lines += [f"**Summary**: {pr_review.summary}", ""]

    lines += [
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

    body: list[str] = []
    for review in sorted(pr_review.file_reviews, key=lambda r: r.filepath):
        if not review.ai_comments and not review.degraded:
            continue
        body.append(f"## `{review.filepath}`")
        body.append("")
        body.append(f"**Rating**: {review.overall_rating.value}")
        body.append("")
        if review.summary:
            body += [review.summary, ""]
        if review.degraded:
            body += [
                "> Part of this file could not be reviewed; findings may be incomplete.",
                "",
            ]

        for severity in (Severity.ERROR, Severity.WARNING, Severity.INFO):
            group = [c for c in review.ai_comments if c.severity is severity]
            if not group:
                continue
            body.append(f"### {_HEADING[severity]}")
            body.append("")
            for comment in sorted(
                group, key=lambda c: min((ln.line_number for ln in c.line_numbers), default=0)
            ):
                cited = ", ".join(str(ln.line_number) for ln in comment.line_numbers)
                body.append(
                    f"- **Issue type**: {comment.severity.value} (Line {cited}) "
                    f"[{comment.criteria}]"
                )
                body.append(f"  {comment.message}")
                if comment.suggestion:
                    body.append(f"  - **Suggestion**: {comment.suggestion}")
                if comment.implementation_notes:
                    body.append(f"  - **Notes**: {comment.implementation_notes}")
            body.append("")

    if body:
        lines += ["## Detailed review results", ""] + body
    else:
        lines += ["No defects found in the added code.", ""]

    lines += ["---", ""]
    diagnostics = [f"- Review id: {pr_review.cc_id}"]
    if commit:
        diagnostics.append(f"- Reviewer build: `{commit}`")
    degraded = [r.filepath for r in pr_review.file_reviews if r.degraded]
    if degraded:
        diagnostics.append(f"- Degraded files: {', '.join(degraded)}")
    lines += [
        "<details><summary>Diagnostics</summary>",
        "",
        *diagnostics,
        "",
        "</details>",
    ]
    return "\n".join(lines).rstrip() + "\n"
