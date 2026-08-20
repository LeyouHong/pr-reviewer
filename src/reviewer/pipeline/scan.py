"""Cron-facing scanner.

A ``scan`` invocation walks every repository in ``settings.json``, decides
which open PRs still need attention, and runs the review pipeline against
them. The decision is deliberately conservative: any signal that we might
already be up to date makes us skip. Missing one review cycle is cheap; a
duplicate review right after the author pushed is noisy.

Skip / review decision, per PR:

1. ``!do-not-review`` in any comment → skip forever.
2. ``!review`` newer than the last posted report → force review.
3. Report exists newer than the PR's last-update time → skip.
4. Otherwise → review.

Cron itself is not our problem — a systemd timer or a crontab entry drives
``pr-reviewer scan`` at whatever cadence the operator prefers. Overlap is,
though: the decision to review is made before a review that takes minutes, and
the evidence that would suppress a second one — a posted report — only exists
after it finishes. Two sweeps started ten minutes apart would both decide the
same pull request needs a review and both post one.

A sweep therefore holds an exclusive lock for its whole duration and a second
invocation exits rather than queueing. That covers one host, which is what
cron overlap is; two machines scanning the same repository would need a claim
the remote side can see, and this does not provide one.
"""

from __future__ import annotations

import logging
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Callable
from zoneinfo import ZoneInfo

from ..config import Config
from ..locking import LockHeld, exclusive
from ..settings import RepoConfig, ScanSettings, WindowConfig
from ..timestamps import is_after, newest
from ..sources import github
from ..sources.worktree import WorktreePool, WorktreeError

log = logging.getLogger(__name__)


def _resolve_overrides(settings: ScanSettings) -> WindowConfig | None:
    now = datetime.now(tz=ZoneInfo(settings.operation.timezone))
    return settings.operation.window_for(now)


def _scoped_to_checkout(
    config: Config, repo: RepoConfig, pr: dict, pool: WorktreePool | None
) -> tuple[Config, str | None]:
    """Point the file-reading stages at this pull request's own tree.

    Returns the config to review with, plus the worktree path to clean up
    afterwards. When the repository declares no checkout, the stages that would
    read files are switched off instead of being left aimed at the scanner's
    working directory: a validator reading an unrelated repository does not
    fail, it returns confident verdicts about code that has nothing to do with
    the change.
    """
    if pool is None:
        return (
            replace(config, enable_validation=False, agentic_review=False),
            None,
        )

    number = int(pr["number"])
    head = pr.get("headRefOid") or ""
    name = f"{repo.url}#{number}"
    if head and not pool.has(head):
        pool.fetch(f"+refs/pull/{number}/head:refs/remotes/origin/pr/{number}")
    try:
        path = pool.checkout(name, head)
    except WorktreeError as exc:
        log.warning(
            "scan: no checkout for %s (%s); reviewing the diff without file access",
            name,
            exc,
        )
        return (
            replace(config, enable_validation=False, agentic_review=False),
            None,
        )
    return replace(config, repo_path=path), name


def _apply_overrides(config: Config, window: WindowConfig | None) -> Config:
    if window is None or window.max_files is None:
        return config
    # ``replace`` on a dataclass returns a new instance; the caller's original
    # Config is left untouched so a later PR in the same scan is not stuck with
    # this window's cap if the window boundary crosses mid-scan.
    return replace(config, max_files=window.max_files)


def _should_review(
    pr: dict,
    comments: list[dict],
    reports: list[dict],
) -> tuple[bool, str]:
    """Return ``(review?, reason)`` for one open PR.

    The reason string is what the scan log prints, so it doubles as the
    operator's audit trail: "why did we (not) review PR X on this pass?".
    """
    # A trigger is consumed by the report that answers it. Without this bound
    # one ``!review`` comment re-fires on every sweep forever, because the
    # comment never goes away.
    answered_at = _newest_report_at(reports)
    trigger = github.trigger_marker(comments, since_iso=answered_at or None)
    if trigger == "!do-not-review":
        return False, "opted out via !do-not-review"
    if trigger == "!review":
        return True, "explicit !review trigger"

    updated_at = pr.get("updatedAt") or ""
    if updated_at and _newest_report_after(reports, updated_at):
        return False, f"already reviewed after last update ({updated_at})"

    return True, "no fresh report"


def _newest_report_at(reports: list[dict]) -> str:
    """Timestamp of the most recent report, or ``""`` when none exists."""
    return newest([r.get("created_at") or "" for r in reports])


def _newest_report_after(reports: list[dict], iso: str) -> bool:
    latest = _newest_report_at(reports)
    return bool(latest) and is_after(latest, iso)


def scan_repos(
    settings: ScanSettings,
    config: Config,
    *,
    dry_run: bool = False,
    reviewer: Callable[[Config, str, int], int] | None = None,
    lock_path: Path | None = None,
) -> list[dict]:
    """Iterate every configured repo and post reviews where needed.

    Held under an exclusive lock unless ``dry_run`` — a dry run posts nothing,
    so there is nothing to double-post and no reason to make an operator
    inspecting the decisions wait on a live sweep. Raises
    :class:`reviewer.locking.LockHeld` when another sweep is already running.

    ``reviewer`` is injected so tests can drive the loop without a real
    pipeline; when ``None``, the default implementation runs the full
    ``ReviewPipeline`` and posts as an issue comment. Callers who want the
    inline flavour or a dry-run rendering supply their own hook.
    """
    if dry_run:
        return _sweep(settings, config, dry_run=True, reviewer=reviewer)

    path = lock_path or default_lock_path()
    with exclusive(path, label="scan"):
        return _sweep(settings, config, dry_run=False, reviewer=reviewer)


def _sweep(
    settings: ScanSettings,
    config: Config,
    *,
    dry_run: bool,
    reviewer: Callable[[Config, str, int], int] | None,
) -> list[dict]:
    window = _resolve_overrides(settings)
    scoped_config = _apply_overrides(config, window)

    results: list[dict] = []
    for repo in settings.repositories:
        try:
            prs = github.list_open_prs(repo.url, repo.target_branches)
        except github.GitHubError as exc:
            log.error("scan: failed to list PRs for %s: %s", repo.url, exc)
            continue

        pool: WorktreePool | None = None
        if repo.checkout is not None:
            try:
                pool = WorktreePool(repo.checkout, repo.checkout.parent / ".pr-reviewer-worktrees")
            except WorktreeError as exc:
                log.error("scan: %s declares checkout %s but %s", repo.url, repo.checkout, exc)
        elif config.enable_validation or config.agentic_review:
            log.warning(
                "scan: %s has no `checkout` in settings; validation and agentic "
                "review are disabled for it so no stage reads an unrelated tree",
                repo.url,
            )

        log.info("scan: %s -> %d open PR(s)", repo.url, len(prs))
        for pr in prs:
            number = int(pr["number"])
            comments = github._pr_issue_comments(number, repo=repo.url)
            reports = [c for c in comments if _is_report(c)]
            do_review, reason = _should_review(pr, comments, reports)
            entry = {
                "repo": repo.url,
                "number": number,
                "reason": reason,
                "reviewed": False,
            }
            if not do_review:
                log.info("scan: skipping %s#%d: %s", repo.url, number, reason)
                results.append(entry)
                continue

            if dry_run:
                log.info("scan: (dry-run) would review %s#%d: %s", repo.url, number, reason)
                results.append(entry)
                continue

            pr_config, worktree_name = _scoped_to_checkout(
                scoped_config, repo, pr, pool
            )
            entry["repo_path"] = str(pr_config.repo_path)
            try:
                _review_and_post(pr_config, repo.url, number, reviewer)
                entry["reviewed"] = True
            except Exception as exc:  # noqa: BLE001 - one PR must not sink the sweep
                log.error("scan: review of %s#%d failed: %s", repo.url, number, exc)
                entry["error"] = str(exc)
            finally:
                if pool is not None and worktree_name is not None:
                    pool.release(worktree_name)
            results.append(entry)

        if pool is not None:
            pool.cleanup()
    return results


def _is_report(comment: dict) -> bool:
    from ..constants import REPORT_FINGERPRINT

    return REPORT_FINGERPRINT in (comment.get("body") or "")


def _review_and_post(
    config: Config,
    repo: str,
    number: int,
    reviewer: Callable[[Config, str, int], int] | None,
) -> int:
    if reviewer is not None:
        return reviewer(config, repo, number)

    from .orchestrator import ReviewPipeline

    info, _diff, _sha = github.load_pull_request_raw(number, repo)
    if not info.changes:
        log.info("scan: %s#%d has no reviewable changes; skipping post", repo, number)
        return 0
    pipeline = ReviewPipeline(config)
    review = pipeline.run(info)
    markdown = pipeline.render(review)

    # Leave room for the report about to be posted, so the PR settles at
    # ``max_reviews`` rather than one above it.
    pruned = github.prune_old_reports(
        number, keep=max(config.max_reviews - 1, 0), repo=repo
    )
    if pruned:
        log.info("scan: pruned %d stale report(s) on %s#%d", pruned, repo, number)
    github.post_report(number, markdown, repo=repo)
    log.info("scan: posted review to %s#%d", repo, number)
    return 1


def default_settings_path() -> Path:
    return Path.cwd() / "settings.json"


def default_lock_path() -> Path:
    """State lives beside the settings file, so one state dir is one scanner."""
    return Path.cwd() / ".pr-reviewer" / "scan.lock"


__all__ = [
    "LockHeld",
    "scan_repos",
    "default_settings_path",
    "default_lock_path",
]
