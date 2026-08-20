"""End-to-end pipeline: review, validate, summarise."""

from __future__ import annotations

import logging

from ..config import Config
from ..diffing.parser import snippet_for_lines
from ..diffing.scope import filter_in_scope
from ..models import (
    CodeChangeInfo,
    FileChange,
    FileChangeReview,
    OverallRating,
    PullRequestReview,
    QualifyVerdict,
    Severity,
    ValidateVerdict,
)
from ..policy import PolicyRouter
from ..prompt import PromptLibrary
from ..provider import make_client
from ..tools.fs_tools import FileSystemTools
from .apply import ValidationOutcome, apply_verdict
from .qualify import Qualifier
from .render import Summarizer, to_markdown
from .review import FileReviewer
from .semgrep import collect_findings as semgrep_findings
from .validate import Validator

log = logging.getLogger(__name__)


class ReviewPipeline:
    def __init__(self, config: Config):
        self.config = config
        self.library = PromptLibrary(
            config.resources_dir, role_variant=config.role_variant
        )
        self.router = PolicyRouter(config.resources_dir)
        self.client = make_client(config)
        self.tools = FileSystemTools(config.repo_path)

        self.reviewer = FileReviewer(
            self.client, self.library, self.router, config, self.tools
        )
        self.qualifier = Qualifier(self.client, self.library)
        self.validator = Validator(self.client, self.library, self.tools)
        self.summarizer = Summarizer(self.client, self.library)

    # -- public -----------------------------------------------------------

    def run(self, info: CodeChangeInfo) -> PullRequestReview:
        changes = self._selectable(info.changes)
        log.info("reviewing %d file(s)", len(changes))

        pr_review = PullRequestReview(
            cc_id=info.cc_id,
            head_sha=info.head_sha,
            total_files=len(info.changes),
            skipped_files=len(info.changes) - len(changes),
        )

        # Semgrep runs once, over every changed file — cheaper than N per-file
        # subprocess spawns, and keeps the rule engine's cross-file view intact
        # for future rule packs that need it.
        semgrep_by_file = self._semgrep(changes)

        for change in changes:
            log.info("reviewing %s", change.filepath)
            review = self.reviewer.review_file(change, info)
            self._merge_semgrep(review, change, semgrep_by_file.get(change.filepath, []))
            if self.config.enable_validation:
                review = self._validate(review, change, info)
            pr_review.file_reviews.append(review)

        if any(r.overall_rating is OverallRating.POOR for r in pr_review.file_reviews):
            pr_review.overall_rating = OverallRating.POOR
        elif pr_review.error_count:
            pr_review.overall_rating = OverallRating.NEEDS_IMPROVEMENT

        pr_review.summary = self.summarizer.overall_summary(pr_review, info)
        return pr_review

    def render(self, pr_review: PullRequestReview, commit: str = "") -> str:
        return to_markdown(pr_review, commit=commit)

    # -- internals --------------------------------------------------------

    def _semgrep(self, changes: list[FileChange]) -> dict[str, list]:
        if not self.config.semgrep_enabled:
            return {}
        options = self.router.semgrep_options()
        if not options.enabled:
            log.info("semgrep enabled but policy has no rules; skipping")
            return {}
        try:
            return semgrep_findings(changes, self.config.repo_path, options)
        except Exception as exc:  # noqa: BLE001 - semgrep failures must not sink the review
            log.error("semgrep pass failed: %s", exc)
            return {}

    def _merge_semgrep(
        self, review: FileChangeReview, change: FileChange, findings: list
    ) -> None:
        if not findings:
            return
        # Scope-check semgrep findings the same way LLM comments are checked.
        # A semgrep hit on an unchanged line is a real defect that predates the
        # PR — noise on a review of *this* change.
        kept, dropped = filter_in_scope(change, list(findings))
        review.ai_comments.extend(kept)
        review.out_of_scope_comments.extend(dropped)
        if kept and any(c.severity is Severity.ERROR for c in kept):
            if review.overall_rating is OverallRating.GOOD:
                review.overall_rating = OverallRating.NEEDS_IMPROVEMENT

    def _selectable(self, changes: list[FileChange]) -> list[FileChange]:
        keep = [
            c
            for c in changes
            if c.status != "deleted"
            and c.hunks
            and not self.router.excluded(c.filepath)
        ]
        if len(keep) > self.config.max_files:
            log.warning(
                "truncating %d changed files to max_files=%d",
                len(keep),
                self.config.max_files,
            )
        return keep[: self.config.max_files]

    def _validate(
        self, review: FileChangeReview, change: FileChange, info: CodeChangeInfo
    ) -> FileChangeReview:
        matched = self.router.match(change.filepath)
        policy_rules = self.library.concat_rules(matched.rule_paths)
        outcome = ValidationOutcome()

        for comment in review.ai_comments:
            cited = {ln.line_number for ln in comment.line_numbers}
            snippet = snippet_for_lines(change, cited)

            verdict = self.qualifier.qualify(comment, change, snippet)
            if verdict is QualifyVerdict.DISCARD:
                log.info("discarded by gate: %s", comment.message[:80])
                outcome.removed.append(comment)
                continue
            if verdict is QualifyVerdict.PASS:
                outcome.retained.append(comment)
                continue

            decided = self.validator.validate(comment, change, snippet, policy_rules)
            log.info("validated %s -> %s", comment.message[:60], decided.value)
            apply_verdict(comment, decided, outcome)

        review.ai_comments = outcome.retained
        if not any(c.severity is Severity.ERROR for c in review.ai_comments):
            if review.overall_rating is OverallRating.NEEDS_IMPROVEMENT:
                review.overall_rating = OverallRating.GOOD

        review.summary = self.summarizer.patch_summary(review, outcome.removed, info)
        return review
