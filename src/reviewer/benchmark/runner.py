"""Benchmark runner with per-PR checkpointing.

Each PR's outcome is written as its own ``part_*.json`` the moment it finishes.
A crashed or interrupted run is resumed by rerunning the same command: parts
already on disk are skipped, so only the missing PRs cost money.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from ..config import Config
from ..diffing.parser import parse_unified_diff, snippet_for_lines
from ..models import CodeChangeInfo
from ..pipeline.orchestrator import ReviewPipeline
from .matcher import MATCHER_VERSION, Matcher
from .model import Corpus, CorpusPr, PrPart, RawFinding
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


def run_benchmark(config: Config, corpus_path: Path, run_dir: Path) -> str:
    corpus = Corpus.model_validate_json(corpus_path.read_text(encoding="utf-8"))
    labelled = [pr for pr in corpus.prs if pr.labelled]
    if not labelled:
        raise SystemExit(
            "No labelled PRs in the corpus. Add ground_truth entries and set "
            '"labelled": true before scoring.'
        )

    run_dir.mkdir(parents=True, exist_ok=True)
    pipeline = ReviewPipeline(config)
    matcher = Matcher(pipeline.client, pipeline.library)

    parts: list[PrPart] = []
    for pr in labelled:
        path = _part_path(run_dir, pr.id)
        if path.exists():
            log.info("resuming: %s already scored", pr.id)
            parts.append(PrPart.model_validate_json(path.read_text(encoding="utf-8")))
            continue

        part = _score_one(pipeline, matcher, pr)
        path.write_text(part.model_dump_json(indent=2), encoding="utf-8")
        parts.append(part)

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
        log.error("benchmark failed for %s: %s", pr.id, exc)
        part.failed = True
        part.error = str(exc)
    return part
