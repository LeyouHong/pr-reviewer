"""Benchmark runner with per-PR checkpointing.

Each PR's outcome is written as its own ``part_*.json`` the moment it finishes.
A crashed or interrupted run is resumed by rerunning the same command: parts
already on disk are skipped, so only the missing PRs cost money.
"""

from __future__ import annotations

import json
import logging
from dataclasses import replace
from pathlib import Path

from ..config import Config
from ..diffing.parser import parse_unified_diff, snippet_for_lines
from ..models import CodeChangeInfo
from ..pipeline.orchestrator import ReviewPipeline
from ..provider.errors import is_billing_failure
from .matcher import MATCHER_VERSION, Matcher
from .model import Corpus, CorpusPr, PrPart, RawFinding
from ..sources.worktree import WorktreePool
from .scoring import format_report, score_parts

log = logging.getLogger(__name__)

# A run that degrades past this fraction is not a result, it is an outage.
MAX_FAILED_FRACTION = 0.2


def _part_path(run_dir: Path, pr_id: str) -> Path:
    safe = pr_id.replace("/", "_")
    return run_dir / f"part_{safe}.json"


def _to_info(pr: CorpusPr) -> CodeChangeInfo:
    return CodeChangeInfo(
        repository=pr.repo,
        cc_id=pr.id,
        cc_title=pr.title,
        cc_description=pr.description,
        source_branch=pr.head_branch,
        target_branch=pr.base_branch,
        changes=parse_unified_diff(pr.diff),
    )


def run_benchmark(
    config: Config,
    corpus_path: Path,
    run_dir: Path,
    checkout_repo: Path | None = None,
) -> str:
    corpus = Corpus.model_validate_json(corpus_path.read_text(encoding="utf-8"))
    labelled = [pr for pr in corpus.prs if pr.labelled]
    if not labelled:
        raise SystemExit(
            "No labelled PRs in the corpus. Add ground_truth entries and set "
            '"labelled": true before scoring.'
        )

    run_dir.mkdir(parents=True, exist_ok=True)

    if config.enable_validation and checkout_repo is None:
        log.warning(
            "validation is on but no --checkout-repo was given: the validator will "
            "read %s, which is not pinned to each PR's commit. Precision from this "
            "run is not comparable to a pinned one.",
            config.repo_path,
        )

    pool = (
        WorktreePool(checkout_repo, run_dir / "worktrees")
        if checkout_repo is not None
        else None
    )
    parts: list[PrPart] = []
    try:
        matcher_pipeline = ReviewPipeline(config)
        matcher = Matcher(matcher_pipeline.client, matcher_pipeline.library)

        for pr in labelled:
            path = _part_path(run_dir, pr.id)
            if path.exists():
                cached = PrPart.model_validate_json(path.read_text(encoding="utf-8"))
                if cached.matcher_version == MATCHER_VERSION:
                    log.info("resuming: %s already scored", pr.id)
                    parts.append(cached)
                    continue
                # Reusing it would report a score under the old judge while
                # labelling the run with the new one. Re-scoring costs money;
                # a scorecard that quietly measures the wrong thing costs more.
                log.warning(
                    "%s was scored by matcher %s, not %s — re-scoring",
                    pr.id,
                    cached.matcher_version,
                    MATCHER_VERSION,
                )

            pipeline = matcher_pipeline
            if pool is not None:
                # A pipeline per PR, because repo_path is what roots the
                # validator's file tools.
                pinned = replace(config, repo_path=pool.checkout(pr.id, pr.pin_commit))
                pipeline = ReviewPipeline(pinned)

            part = _score_one(pipeline, matcher, pr)
            path.write_text(part.model_dump_json(indent=2), encoding="utf-8")
            parts.append(part)
    finally:
        if pool is not None:
            pool.cleanup()

    failed = sum(1 for p in parts if p.failed)
    if parts and failed / len(parts) > MAX_FAILED_FRACTION:
        raise SystemExit(
            f"{failed}/{len(parts)} PRs failed, above the {MAX_FAILED_FRACTION:.0%} "
            "degradation budget. The numbers would be misleading; fix the cause "
            "and rerun."
        )

    overall, by_severity, by_difficulty, by_value = score_parts(parts)
    report = format_report(overall, by_severity, by_difficulty, by_value, parts)
    (run_dir / "report.md").write_text(report, encoding="utf-8")
    (run_dir / "config.json").write_text(
        json.dumps(
            {
                "model": config.model,
                "ensemble_size": config.ensemble_size,
                "enable_validation": config.enable_validation,
                "pinned_checkouts": checkout_repo is not None,
                "matcher_version": MATCHER_VERSION,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    return report


def _score_one(pipeline: ReviewPipeline, matcher: Matcher, pr: CorpusPr) -> PrPart:
    log.info("benchmarking %s", pr.id)
    part = PrPart(
        pr_id=pr.id,
        matcher_version=MATCHER_VERSION,
        ground_truth=list(pr.ground_truth),
    )
    try:
        info = _to_info(pr)
        review = pipeline.run(info)
        changes = {c.filepath: c for c in info.changes}

        for file_review in review.file_reviews:
            change = changes.get(file_review.filepath)
            for comment in file_review.ai_comments:
                lines = [ln.line_number for ln in comment.line_numbers]
                snippet = (
                    snippet_for_lines(change, set(lines)) if change else file_review.diff
                )
                finding = RawFinding(
                    file=file_review.filepath,
                    lines=lines,
                    severity=comment.severity.value,
                    category=comment.category.value,
                    message=comment.message,
                    suggestion=comment.suggestion,
                )
                part.findings.append(matcher.match(finding, pr, snippet))
    except Exception as exc:  # noqa: BLE001 - one bad PR must not sink the run
        if is_billing_failure(exc):
            # Every remaining PR would fail identically. Stop now so the parts
            # already on disk stay usable and the run can resume after a top-up.
            raise SystemExit(
                f"Stopped at {pr.id}: the account cannot pay for further requests. "
                "Top up, then rerun the same command — completed PRs resume from "
                "their checkpoints."
            ) from exc
        log.error("benchmark failed for %s: %s", pr.id, exc)
        part.failed = True
        part.error = str(exc)
    return part
