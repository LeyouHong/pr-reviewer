"""Per-file review: ensemble, scope filtering, and majority-rule aggregation."""

from __future__ import annotations

import json
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date

from .. import constants
from ..config import Config
from ..diffing.chunker import chunk_file_change
from ..diffing.parser import render_diff
from ..diffing.scope import filter_in_scope
from ..models import (
    CodeChangeInfo,
    FileChange,
    FileChangeReview,
    LLMFileChangeReview,
    OverallRating,
    ReviewComment,
    Severity,
)
from ..policy import PolicyRouter
from ..prompt import PromptLibrary, file_changes_block, render
from ..provider import ProviderClient
from ..tools.fs_tools import FileSystemTools
from ..tools.researcher import Researcher, build_dispatch, extended_tool_specs

log = logging.getLogger(__name__)

_TOOL_NAME = "submit_file_review"
_TOOL_DESC = "Submit the structured review of the file change under review."


class FileReviewer:
    def __init__(
        self,
        client: ProviderClient,
        library: PromptLibrary,
        router: PolicyRouter,
        config: Config,
        tools: FileSystemTools | None = None,
    ):
        self._client = client
        self._library = library
        self._router = router
        self._config = config
        self._tools = tools or FileSystemTools(config.repo_path)
        # The researcher is only reached when the outer reviewer calls the
        # ``research_codebase`` tool, so it costs nothing on non-agentic paths
        # even though it is constructed eagerly.
        self._researcher = Researcher(client, library, self._tools)
        self._agent_tool_specs = extended_tool_specs()
        self._agent_dispatch = build_dispatch(self._tools, self._researcher)

    # -- public -----------------------------------------------------------

    def review_file(
        self, change: FileChange, info: CodeChangeInfo
    ) -> FileChangeReview:
        matched = self._router.match(change.filepath)
        rules = self._library.concat_rules(
            [p for p in matched.rule_paths if not p.endswith("general.md")]
        )
        general = self._library.rules("/rules/general.md")

        chunks = chunk_file_change(change)
        all_comments: list[ReviewComment] = []
        all_oos: list[ReviewComment] = []
        summaries: list[str] = []
        metrics = None
        rating = OverallRating.GOOD
        degraded = False

        for index, hunks in enumerate(chunks):
            rendered = render_diff(change, hunks)
            task = (
                "code_review_agent"
                if self._config.agentic_review
                else "code_review_prompt"
            )
            prompt = render(
                self._library.task(task),
                role_prompt=self._library.role("reviewer"),
                mr_project=info.repository,
                mr_title=info.cc_title,
                mr_source_branch=info.source_branch,
                mr_target_branch=info.target_branch,
                review_date=date.today().isoformat(),
                mr_description=info.cc_description or "(no description provided)",
                mr_file_changes=file_changes_block(
                    change.filepath, rendered, chunk_index=index, chunk_total=len(chunks)
                ),
                general_rules=general,
                rules=rules or "(no language-specific rules apply to this file)",
            )

            try:
                review = self._review_chunk(prompt, change, info, rendered)
            except Exception as exc:  # noqa: BLE001 - one bad chunk must not sink the file
                log.error("review failed for %s chunk %d: %s", change.filepath, index, exc)
                degraded = True
                continue

            kept, dropped = filter_in_scope(change, list(review.comments))
            all_comments.extend(kept)
            all_oos.extend(dropped)
            summaries.append(review.summary)
            metrics = metrics or review.metrics
            if review.overall_rating is OverallRating.POOR:
                rating = OverallRating.POOR

        if any(c.severity is Severity.ERROR for c in all_comments):
            rating = OverallRating.NEEDS_IMPROVEMENT if rating is not OverallRating.POOR else rating

        return FileChangeReview(
            filepath=change.filepath,
            diff=render_diff(change),
            ai_comments=all_comments,
            out_of_scope_comments=all_oos,
            overall_rating=rating,
            summary=" ".join(s for s in summaries if s).strip(),
            metrics=metrics,
            degraded=degraded,
        )

    # -- internals --------------------------------------------------------

    def _review_chunk(
        self,
        prompt: str,
        change: FileChange,
        info: CodeChangeInfo,
        rendered: str,
    ) -> LLMFileChangeReview:
        size = max(self._config.ensemble_size, 1)
        if size == 1:
            return self._one_review(prompt, label=f"review:{change.filepath}")

        reviews = self._run_ensemble(prompt, change.filepath, size)
        if not reviews:
            raise RuntimeError("every ensemble member failed")
        if len(reviews) == 1:
            return reviews[0]
        return self._aggregate(reviews, change, info)

    def _one_review(self, prompt: str, label: str) -> LLMFileChangeReview:
        if self._config.agentic_review:
            return self._client.run_agent_structured(
                prompt,
                LLMFileChangeReview,
                tool_specs=self._agent_tool_specs,
                dispatch=self._agent_dispatch,
                result_tool=_TOOL_NAME,
                result_description=_TOOL_DESC,
                max_turns=constants.MAX_TURNS_REVIEW,
                label=label,
            )
        return self._client.complete_structured(
            prompt,
            LLMFileChangeReview,
            tool_name=_TOOL_NAME,
            tool_description=_TOOL_DESC,
            label=label,
        )

    def _run_ensemble(
        self, prompt: str, filepath: str, size: int
    ) -> list[LLMFileChangeReview]:
        reviews: list[LLMFileChangeReview] = []
        with ThreadPoolExecutor(max_workers=size) as pool:
            futures = [
                pool.submit(self._one_review, prompt, f"review:{filepath}:{i}")
                for i in range(size)
            ]
            for future in as_completed(futures, timeout=constants.ENSEMBLE_TIMEOUT_S):
                try:
                    reviews.append(future.result())
                except Exception as exc:  # noqa: BLE001
                    log.warning("ensemble member failed for %s: %s", filepath, exc)
        return reviews

    def _aggregate(
        self,
        reviews: list[LLMFileChangeReview],
        change: FileChange,
        info: CodeChangeInfo,
    ) -> LLMFileChangeReview:
        """Let the model synthesise consensus.

        Counting votes in code loses nuance: one member's ``info`` and
        another's ``warning`` can agree in substance while differing in label.
        """
        blocks = []
        for i, review in enumerate(reviews, start=1):
            body = json.dumps(review.model_dump(mode="json"), indent=2, ensure_ascii=False)
            blocks.append(f"````json agent-{i}\n{body}\n````")

        threshold = (len(reviews) + 1) // 2  # ceil(N/2)
        prompt = render(
            self._library.task("file_change_majority_rule"),
            role_prompt=self._library.role("majority_rule"),
            mr_project=info.repository,
            mr_title=info.cc_title,
            mr_source_branch=info.source_branch,
            mr_target_branch=info.target_branch,
            mr_description=info.cc_description or "(no description provided)",
            filepath=change.filepath,
            review_list="\n\n".join(blocks),
            majority_threshold=threshold,
        )
        return self._client.complete_structured(
            prompt,
            LLMFileChangeReview,
            tool_name=_TOOL_NAME,
            tool_description="Submit the aggregated consensus review.",
            label=f"majority:{change.filepath}",
        )
