"""Capture PRs into a corpus file for hand-labelling."""

from __future__ import annotations

import logging
from pathlib import Path

from ..sources import github
from .model import Corpus, CorpusPr

log = logging.getLogger(__name__)


def load_or_create(path: Path) -> Corpus:
    if path.exists():
        return Corpus.model_validate_json(path.read_text(encoding="utf-8"))
    return Corpus()


def capture(path: Path, repo: str, numbers: list[int]) -> Corpus:
    corpus = load_or_create(path)
    known = {pr.id for pr in corpus.prs}

    for number in numbers:
        pr_id = f"{repo}#{number}"
        if pr_id in known:
            log.info("%s already captured, skipping", pr_id)
            continue
        # Labels are anchored to the commit captured here. Record it, or a
        # later push silently invalidates the ground truth.
        info, diff, pin = github.load_pull_request_raw(number, repo)
        corpus.prs.append(
            CorpusPr(
                id=pr_id,
                repo=repo,
                number=number,
                title=info.cc_title,
                description=info.cc_description,
                base_branch=info.target_branch,
                head_branch=info.source_branch,
                pin_commit=pin,
                diff=diff,
                ground_truth=[],
                labelled=False,
            )
        )
        log.info(
            "captured %s at %s (%d reviewable files)",
            pr_id,
            pin[:8] or "unknown",
            len(info.changes),
        )

    path.write_text(corpus.model_dump_json(indent=2), encoding="utf-8")
    return corpus
