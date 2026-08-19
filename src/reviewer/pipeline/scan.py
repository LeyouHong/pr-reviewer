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
``pr-reviewer scan`` at whatever cadence the operator prefers. The scanner
must be re-entrant: two overlapping invocations must not double-post, which
is why the fingerprint dedup runs before the review, not after.
"""

from __future__ import annotations

import logging
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Callable
from zoneinfo import ZoneInfo

from ..config import Config
from ..settings import ScanSettings, WindowConfig
from ..sources import github

log = logging.getLogger(__name__)


def _resolve_overrides(settings: ScanSettings) -> WindowConfig | None:
    now = datetime.now(tz=ZoneInfo(settings.operation.timezone))
    return settings.operation.window_for(now)


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
    trigger = github.trigger_marker(comments)
    if trigger == "!do-not-review":
        return False, "opted out via !do-not-review"
    if trigger == "!review":
        return True, "explicit !review trigger"

    updated_at = pr.get("updatedAt") or ""
    if updated_at and _newest_report_after(reports, updated_at):
        return False, f"already reviewed after last update ({updated_at})"

    return True, "no fresh report"


def _newest_report_after(reports: list[dict], iso: str) -> bool:
    if not reports:
        return False
    newest = max(r.get("created_at", "") for r in reports)
    return newest > iso


def scan_repos(
    settings: ScanSettings,
    config: Config,
    *,
    dry_run: bool = False,
    reviewer: Callable[[Config, str, int], int] | None = None,
) -> list[dict]:
    """Iterate every configured repo and post reviews where needed.

    ``reviewer`` is injected so tests can drive the loop without a real
    pipeline; when ``None``, the default implementation runs the full
    ``ReviewPipeline`` and posts as an issue comment. Callers who want the
    inline flavour or a dry-run rendering supply their own hook.
    """
    window = _resolve_overrides(settings)
    scoped_config = _apply_overrides(config, window)

    results: list[dict] = []
    for repo in settings.repositories:
        try:
            prs = github.list_open_prs(repo.url, repo.target_branches)
        except github.GitHubError as exc:
            log.error("scan: failed to list PRs for %s: %s", repo.url, exc)
            continue

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

            try:
                _review_and_post(scoped_config, repo.url, number, reviewer)
                entry["reviewed"] = True
            except Exception as exc:  # noqa: BLE001 - one PR must not sink the sweep
                log.error("scan: review of %s#%d failed: %s", repo.url, number, exc)
                entry["error"] = str(exc)
            results.append(entry)
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

    pruned = github.prune_old_reports(number, keep=4, repo=repo)
    if pruned:
        log.info("scan: pruned %d stale report(s) on %s#%d", pruned, repo, number)
    github.post_report(number, markdown, repo=repo)
    log.info("scan: posted review to %s#%d", repo, number)
    return 1


def default_settings_path() -> Path:
    return Path.cwd() / "settings.json"


__all__ = [
    "scan_repos",
    "default_settings_path",
]
