"""3-class judge: does this finding identify one of the known bugs?

The judge returns MATCH / PARTIAL / NO_MATCH; the matcher collapses that to
``matched_gt_id`` for scoring (only MATCH counts as recall, so a PARTIAL still
reads as a false positive in the F1 number). Keeping the raw verdict on the
scored finding lets the report characterise PARTIALs separately — that is
where prompt work pays off, so hiding it in the collapse would waste signal.
"""

from __future__ import annotations

import logging

from ..prompt import PromptLibrary, render
from ..provider import ProviderClient
from .model import CorpusPr, JudgeDecision, JudgeVerdict, RawFinding, ScoredFinding

log = logging.getLogger(__name__)

# Bump when the judge prompt or procedure changes: parts scored by different
# judges must never be pooled into one number.
MATCHER_VERSION = "judge-v1"


class Matcher:
    def __init__(self, client: ProviderClient, library: PromptLibrary):
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
                verdict=JudgeVerdict.NO_MATCH,
                reasoning="PR has no ground-truth bugs; nothing to match.",
            )

        listing = "\n".join(
            f"- id `{gt.id}` ({gt.file}, lines {gt.lines or 'unspecified'}): {gt.description}"
            for gt in pr.ground_truth
        )
        prompt = render(
            self._library.task("judge_issue"),
            filepath=finding.file,
            severity=finding.severity,
            message=finding.message,
            suggestion=finding.suggestion or "(none)",
            ground_truth=listing,
            diff_snippet=diff_snippet,
        )
        decision = self._client.complete_structured(
            prompt,
            JudgeDecision,
            tool_name="submit_judgment",
            tool_description="Submit the 3-class match judgment.",
            label=f"judge:{pr.id}",
        )

        valid = {gt.id for gt in pr.ground_truth}
        matched = decision.matched_gt_id
        verdict = decision.verdict

        if matched is not None and matched not in valid:
            log.warning("judge invented gt id %r; treating as no-match", matched)
            matched = None
            verdict = JudgeVerdict.NO_MATCH

        # Enforce the collapse contract: only a clean MATCH counts toward
        # recall. A PARTIAL keeps its matched_gt_id for characterisation but
        # is not paired to the ground truth in the scorecard.
        binary_matched = matched if verdict is JudgeVerdict.MATCH else None

        return ScoredFinding(
            **finding.model_dump(),
            matched_gt_id=binary_matched,
            verdict=verdict,
            reasoning=decision.reasoning,
        )
