"""Drains the queue, one revision at a time.

Separate process from the receiver so a review that takes ten minutes cannot
delay a webhook that must answer in ten seconds, and so the two scale and
restart independently.

Every job passes the same posted-report check the sweep uses before any model
is called. Belt and braces on purpose: the queue prevents a revision being
*queued* twice, and the check prevents it being *reviewed* twice when the
queue's own record was lost — a reclaimed job from a worker that died after
posting, a wiped state directory, a second worker started by mistake. Money and
a duplicate comment ride on that, and the check costs one API call.
"""

from __future__ import annotations

import logging
import time
from dataclasses import replace
from pathlib import Path

from ..config import Config
from ..exception_handling import DegradedCall, is_billing_failure
from ..sources import github
from ..sources.checkout import CheckoutProvider
from .queue import Job, JobQueue

log = logging.getLogger(__name__)

# A job that keeps failing is poison; past this it goes to `failed` for a human
# rather than cycling forever and spending on every attempt.
DEFAULT_MAX_ATTEMPTS = 3


def review_job(
    config: Config,
    job: Job,
    *,
    inline: bool = True,
    checkouts: CheckoutProvider | None = None,
) -> str:
    """Review one revision and post the report. Returns a short outcome.

    ``checkouts`` pins the file-reading stages to this job's own commit. A
    worker draining a queue is the case where a single fixed ``repo_path`` is
    most obviously wrong: consecutive jobs are different pull requests, often
    different repositories, and the checkout that was right for one is wrong
    for the next.
    """
    if github.has_report_for(job.number, job.head_sha, repo=job.repo):
        return "already reviewed"

    from ..pipeline.inline import build_inline_review
    from ..pipeline.orchestrator import ReviewPipeline

    info, _diff, head = github.load_pull_request_raw(job.number, job.repo)
    if head and head != job.head_sha:
        # The branch moved while this job waited. Reviewing the new head under
        # the old job's identity would file the report against the wrong
        # revision, so let the newer job — which the receiver already queued —
        # handle it.
        return f"superseded: head is now {head[:8]}"
    if not info.changes:
        return "no reviewable changes"

    provider = checkouts or CheckoutProvider()
    with provider.pinned(config, job.repo, job.number, job.head_sha) as pinned:
        pipeline = ReviewPipeline(pinned)
        review = pipeline.run(info)

    keep = max(config.max_reviews - 1, 0)
    github.prune_old_reports(job.number, keep=keep, repo=job.repo)

    if inline:
        payload = build_inline_review(review, commit=config.build_stamp)
        github.post_inline_review(
            job.number,
            job.head_sha,
            payload.body,
            [
                {"path": c.path, "line": c.line, "side": c.side, "body": c.body}
                for c in payload.comments
            ],
            repo=job.repo,
        )
    else:
        github.post_report(job.number, pipeline.render(review, commit=config.build_stamp), repo=job.repo)

    return f"posted {review.total_comments} finding(s)"


def run_once(config: Config, queue: JobQueue, *, inline: bool = True,
             max_attempts: int = DEFAULT_MAX_ATTEMPTS,
             checkouts: CheckoutProvider | None = None) -> bool:
    """Handle at most one job. Returns whether there was one."""
    claimed = queue.claim()
    if claimed is None:
        return False
    job, path = claimed

    try:
        outcome = review_job(config, job, inline=inline, checkouts=checkouts)
        log.info("worker: %s — %s", job.label, outcome)
        queue.complete(job, path)
    except SystemExit as exc:
        # The billing abort. Requeue rather than fail: the work is still valid
        # and will run once someone tops up.
        queue.requeue(job, path)
        raise
    except DegradedCall as exc:
        queue.fail(job, path, f"degraded: {exc}")
    except Exception as exc:  # noqa: BLE001 - one bad job must not stop the drain
        if is_billing_failure(exc):
            queue.requeue(job, path)
            raise SystemExit(
                f"worker: stopping at {job.label} — the account cannot pay for "
                "further requests. Top up and restart; the job is still queued."
            ) from exc
        if job.attempts + 1 >= max_attempts:
            queue.fail(job, path, f"{type(exc).__name__}: {exc}")
        else:
            queue.requeue(job, path)
    return True


def drain(config: Config, queue: JobQueue, *, inline: bool = True,
          idle_sleep_s: float = 5.0, once: bool = False,
          checkouts: CheckoutProvider | None = None) -> int:
    """Work the queue until it is empty (``once``) or forever.

    Polls rather than watches: a five-second wait on an empty queue is
    invisible next to a review, and it removes a dependency on filesystem
    notification behaving the same on every platform.
    """
    provider = checkouts or CheckoutProvider()
    handled = 0
    try:
        while True:
            if run_once(config, queue, inline=inline, checkouts=provider):
                handled += 1
                continue
            if once:
                return handled
            time.sleep(idle_sleep_s)
    finally:
        provider.cleanup()


__all__ = ["DEFAULT_MAX_ATTEMPTS", "drain", "review_job", "run_once"]
