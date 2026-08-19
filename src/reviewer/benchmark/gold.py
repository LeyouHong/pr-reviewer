"""N-vote gold labelling.

The judge is itself an LLM: two calls with the same input can disagree, and
the disagreements are informative. A scorecard produced by a judge whose
same-input verdict flips run-to-run is not a scorecard, it is noise.

Gold labelling runs the judge *N* independent passes over the findings on
disk and settles each by unanimous vote. A finding all passes label
identically is "confident" and can be trusted without human review; a
finding with any disagreement is left as ``TODO``, and the per-pass votes
are surfaced so a person can adjudicate one time and then the label is
frozen.

Two calls the same, three or five passes, no async orchestration: this is
the minimum viable slice. When it makes sense to parallelise, the
:class:`GoldLabeller` interface will not have to change.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from pydantic import BaseModel, Field

from ..config import Config
from ..diffing.parser import parse_unified_diff, snippet_for_lines
from ..pipeline.orchestrator import ReviewPipeline
from .matcher import MATCHER_VERSION, Matcher
from .model import (
    Corpus,
    CorpusPr,
    JudgeVerdict,
    PrPart,
    RawFinding,
    ScoredFinding,
)

log = logging.getLogger(__name__)

# Sentinel for a finding whose passes disagreed. Distinct from
# ``JudgeVerdict.NO_MATCH`` because the two mean different things: NO_MATCH
# is "the judge said no", TODO is "the judges couldn't agree".
UNSETTLED = "TODO"


class GoldVote(BaseModel):
    """One pass's verdict on one finding."""

    pass_index: int
    verdict: JudgeVerdict
    matched_gt_id: Optional[str] = None
    reasoning: str = ""


class GoldFinding(BaseModel):
    """A finding after N gold passes.

    ``settled_verdict`` is the string ``"TODO"`` when the passes disagreed,
    otherwise the string form of the unanimous :class:`JudgeVerdict`.
    Keeping the sentinel and the enum values in the same field lets the
    consumer branch on one comparison instead of two.
    """

    finding_id: str
    pr_id: str
    file: str
    severity: str
    message: str
    votes: list[GoldVote] = Field(default_factory=list)
    settled_verdict: str = UNSETTLED
    settled_gt_id: Optional[str] = None


class GoldWorklist(BaseModel):
    matcher_version: str
    passes: int
    findings: list[GoldFinding] = Field(default_factory=list)

    @property
    def confident(self) -> list[GoldFinding]:
        return [f for f in self.findings if f.settled_verdict != UNSETTLED]

    @property
    def unsettled(self) -> list[GoldFinding]:
        return [f for f in self.findings if f.settled_verdict == UNSETTLED]


@dataclass
class GoldLabeller:
    """Coordinates N judge passes and vote-merge.

    Kept a small dataclass rather than a bag of module functions so the
    caller can hold one object across passes — the ``Matcher`` and the
    corpus lookup are re-used, not rebuilt per finding.
    """

    matcher: Matcher
    corpus: dict[str, CorpusPr]
    passes: int = 3

    def label(self, part: PrPart) -> list[GoldFinding]:
        pr = self.corpus.get(part.pr_id)
        if pr is None:
            log.warning("gold: part %s has no matching corpus PR; skipping", part.pr_id)
            return []

        changes = {c.filepath: c for c in parse_unified_diff(pr.diff)}
        results: list[GoldFinding] = []
        for index, scored in enumerate(part.findings):
            finding_id = f"{part.pr_id}#{index}"
            change = changes.get(scored.file)
            snippet = (
                snippet_for_lines(change, set(scored.lines)) if change else ""
            )
            raw = RawFinding(
                file=scored.file,
                lines=list(scored.lines),
                severity=scored.severity,
                category=scored.category,
                message=scored.message,
                suggestion=scored.suggestion,
            )
            votes = self._run_passes(raw, pr, snippet)
            results.append(
                _settle(
                    finding_id=finding_id,
                    pr_id=part.pr_id,
                    raw=raw,
                    votes=votes,
                )
            )
        return results

    def _run_passes(
        self, raw: RawFinding, pr: CorpusPr, snippet: str
    ) -> list[GoldVote]:
        votes: list[GoldVote] = []
        for pass_index in range(1, self.passes + 1):
            try:
                scored: ScoredFinding = self.matcher.match(raw, pr, snippet)
            except Exception as exc:  # noqa: BLE001 - one pass failure must not sink the finding
                log.warning(
                    "gold: pass %d failed for %s / %s: %s",
                    pass_index,
                    pr.id,
                    raw.file,
                    exc,
                )
                continue
            votes.append(
                GoldVote(
                    pass_index=pass_index,
                    verdict=scored.verdict,
                    matched_gt_id=scored.matched_gt_id
                    if scored.verdict is JudgeVerdict.MATCH
                    else None,
                    reasoning=scored.reasoning,
                )
            )
        return votes


def _settle(
    *,
    finding_id: str,
    pr_id: str,
    raw: RawFinding,
    votes: list[GoldVote],
) -> GoldFinding:
    """Unanimous vote settles; anything else is TODO.

    Not a majority vote — a 2/3 split still tells us the judge is unstable on
    this finding, and settling on the majority hides that from anyone reading
    the gold file. The threshold is stricter than fortinac's on purpose: this
    is a small corpus, so gold quality matters more than gold size.
    """
    base = GoldFinding(
        finding_id=finding_id,
        pr_id=pr_id,
        file=raw.file,
        severity=raw.severity,
        message=raw.message,
        votes=votes,
    )
    if not votes:
        return base

    verdicts = {vote.verdict for vote in votes}
    if len(verdicts) != 1:
        return base

    verdict = next(iter(verdicts))
    matched_ids = {vote.matched_gt_id for vote in votes}
    # A unanimous MATCH must also agree on which bug: two passes matching
    # different ids is disagreement dressed as agreement.
    if verdict is JudgeVerdict.MATCH and len(matched_ids) != 1:
        return base

    base.settled_verdict = verdict.value
    if verdict is JudgeVerdict.MATCH:
        base.settled_gt_id = next(iter(matched_ids))
    return base


def label_run(
    config: Config,
    corpus_path: Path,
    run_dir: Path,
    *,
    passes: int = 3,
) -> GoldWorklist:
    """Run ``passes`` judge passes over every part on disk and merge the votes."""
    corpus = Corpus.model_validate_json(corpus_path.read_text(encoding="utf-8"))
    corpus_by_id = {pr.id: pr for pr in corpus.prs}

    parts_paths = sorted(run_dir.glob("part_*.json"))
    if not parts_paths:
        raise SystemExit(
            f"No part_*.json files under {run_dir}. Run `benchmark-run` first."
        )

    pipeline = ReviewPipeline(config)
    matcher = Matcher(pipeline.client, pipeline.library)
    labeller = GoldLabeller(matcher=matcher, corpus=corpus_by_id, passes=passes)

    worklist = GoldWorklist(matcher_version=MATCHER_VERSION, passes=passes)
    for path in parts_paths:
        part = PrPart.model_validate_json(path.read_text(encoding="utf-8"))
        if part.failed:
            continue
        worklist.findings.extend(labeller.label(part))
    return worklist


def format_worklist(worklist: GoldWorklist) -> str:
    """Human-readable summary of the gold pass, for stdout / the report file."""
    total = len(worklist.findings)
    confident = len(worklist.confident)
    lines = [
        "# Gold labelling",
        "",
        f"- matcher version: `{worklist.matcher_version}`",
        f"- passes per finding: {worklist.passes}",
        f"- findings labelled: {total}",
        f"- confident (unanimous): {confident}",
        f"- unsettled (TODO): {total - confident}",
        "",
    ]
    if worklist.unsettled:
        lines += ["## Unsettled findings", ""]
        for finding in worklist.unsettled:
            lines.append(f"### {finding.finding_id}")
            lines.append("")
            lines.append(f"- file: `{finding.file}`")
            lines.append(f"- severity: {finding.severity}")
            lines.append(f"- message: {finding.message}")
            lines.append("")
            lines.append("| pass | verdict | gt id | reasoning |")
            lines.append("|---|---|---|---|")
            for vote in finding.votes:
                reasoning = (vote.reasoning or "").replace("\n", " ")[:200]
                lines.append(
                    f"| {vote.pass_index} | {vote.verdict.value} | "
                    f"`{vote.matched_gt_id}` | {reasoning} |"
                )
            lines.append("")
    return "\n".join(lines) + "\n"
