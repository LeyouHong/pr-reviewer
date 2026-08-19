"""Binary matcher: did this finding identify one of the known bugs?"""

from __future__ import annotations

import logging

from ..prompt import PromptLibrary, render
from ..provider import DeepSeekClient
from .model import CorpusPr, MatchDecision, RawFinding, ScoredFinding

log = logging.getLogger(__name__)

# Bump when the matcher prompt or procedure changes: parts scored by different
# matchers must never be pooled into one number.
MATCHER_VERSION = "binary-v1"


class Matcher:
    def __init__(self, client: DeepSeekClient, library: PromptLibrary):
        self._client = client
        self._library = library

    def match(
        self, finding: RawFinding, pr: CorpusPr, diff_snippet: str
    ) -> ScoredFinding:
        # A PR with no known bugs needs no LLM call: every finding is unmatched.
        if not pr.ground_truth:
            return ScoredFinding(
                **finding.model_dump(),
                matched_gt_id=None,
                reasoning="PR has no ground-truth bugs; nothing to match.",
            )

        listing = "\n".join(
            f"- id `{gt.id}` ({gt.file}, lines {gt.lines or 'unspecified'}): {gt.description}"
            for gt in pr.ground_truth
        )
        prompt = render(
            self._library.task("benchmark_match"),
            filepath=finding.file,
            severity=finding.severity,
            message=finding.message,
            suggestion=finding.suggestion or "(none)",
            ground_truth=listing,
            diff_snippet=diff_snippet,
        )
        decision = self._client.complete_structured(
            prompt,
            MatchDecision,
            tool_name="submit_match",
            tool_description="Submit the binary match decision.",
            label=f"match:{pr.id}",
        )

        valid = {gt.id for gt in pr.ground_truth}
        matched = decision.matched_gt_id
        if matched is not None and matched not in valid:
            log.warning("matcher invented gt id %r; treating as unmatched", matched)
            matched = None

        return ScoredFinding(
            **finding.model_dump(), matched_gt_id=matched, reasoning=decision.reasoning
        )
