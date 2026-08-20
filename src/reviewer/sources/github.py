"""GitHub PR source, driven through the ``gh`` CLI.

Using ``gh`` rather than raw REST means the tool inherits whatever auth the
developer already has, and there is no token to store or refresh.
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
from datetime import datetime, timezone

from ..constants import REPORT_FINGERPRINT
from ..diffing.parser import parse_unified_diff
from ..timestamps import is_after, parse_iso
from ..models import CodeChangeInfo

log = logging.getLogger(__name__)

_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


class GitHubError(RuntimeError):
    pass


def _gh(*args: str, stdin: str | None = None) -> str:
    result = subprocess.run(
        ["gh", *args],
        capture_output=True,
        text=True,
        input=stdin,
        check=False,
    )
    if result.returncode != 0:
        raise GitHubError(f"gh {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout


def load_pull_request_raw(
    number: int, repo: str | None = None
) -> tuple[CodeChangeInfo, str, str]:
    """Fetch a PR once and return (parsed info, raw diff, head commit).

    Callers that need the raw patch text — corpus capture, for one — use this
    so the diff is not fetched twice.
    """
    scope = ["--repo", repo] if repo else []
    meta = json.loads(
        _gh(
            "pr",
            "view",
            str(number),
            *scope,
            "--json",
            "number,title,body,headRefName,baseRefName,headRefOid,headRepository,url",
        )
    )
    # Plain `gh pr diff` returns the combined base..head diff. `--patch` returns
    # a format-patch *series* — one diff per commit — so a file touched in three
    # commits appears three times, with line numbers from intermediate states
    # that do not exist in the merged result.
    diff = _gh("pr", "diff", str(number), *scope)

    project = repo or (meta.get("headRepository") or {}).get("name") or "unknown"
    info = CodeChangeInfo(
        repository=project,
        cc_id=str(meta["number"]),
        cc_title=meta.get("title") or "",
        cc_description=meta.get("body") or "",
        source_branch=meta.get("headRefName") or "",
        target_branch=meta.get("baseRefName") or "",
        changes=parse_unified_diff(diff),
    )
    return info, diff, meta.get("headRefOid") or ""


def load_pull_request(number: int, repo: str | None = None) -> CodeChangeInfo:
    info, _diff, _sha = load_pull_request_raw(number, repo)
    return info


# -- posting & dedup -------------------------------------------------------


def _repo_slug(repo: str | None) -> str:
    if repo:
        return repo
    meta = json.loads(_gh("repo", "view", "--json", "nameWithOwner"))
    return meta["nameWithOwner"]


def existing_report_comments(number: int, repo: str | None = None) -> list[dict]:
    """Prior reports from this tool, identified by the fingerprint marker."""
    slug = _repo_slug(repo)
    raw = _gh(
        "api",
        f"repos/{slug}/issues/{number}/comments",
        "--paginate",
        "--jq",
        ".[] | {id: .id, body: .body, created_at: .created_at}",
    )
    out = []
    for line in raw.splitlines():
        if not line.strip():
            continue
        item = json.loads(line)
        if REPORT_FINGERPRINT in (item.get("body") or ""):
            out.append(item)
    return out


def has_report_since(number: int, since_iso: str, repo: str | None = None) -> bool:
    """True when a report was already posted after ``since_iso``.

    Re-review is driven by new commits, not by the clock: if the newest report
    is younger than the head commit, the author has not pushed since.
    """
    if not since_iso:
        return False
    return any(
        is_after(item.get("created_at"), since_iso)
        for item in existing_report_comments(number, repo)
    )


def prune_old_reports(number: int, keep: int, repo: str | None = None) -> int:
    """Delete all but the newest ``keep`` prior reports. Returns count deleted."""
    slug = _repo_slug(repo)
    comments = existing_report_comments(number, repo)
    comments.sort(key=lambda c: (parse_iso(c.get("created_at")) or _EPOCH))
    stale = comments[: max(len(comments) - keep, 0)]
    for item in stale:
        _gh("api", "-X", "DELETE", f"repos/{slug}/issues/comments/{item['id']}")
    return len(stale)


def post_report(number: int, body: str, repo: str | None = None) -> None:
    scope = ["--repo", repo] if repo else []
    _gh("pr", "comment", str(number), *scope, "--body-file", "-", stdin=body)


def post_inline_review(
    number: int,
    commit_sha: str,
    body: str,
    comments: list[dict],
    repo: str | None = None,
    event: str = "COMMENT",
) -> None:
    """Post a review with per-line comments in one call.

    Uses the ``/pulls/{n}/reviews`` endpoint rather than one POST per comment:
    a single review groups everything under one collapsible thread in the PR
    UI and, more practically, races cleanly against a re-review — either the
    whole batch lands or none of it does.

    ``event`` stays ``COMMENT`` by default. Any auto-review that could
    ``APPROVE`` or ``REQUEST_CHANGES`` on someone else's code would be a
    social hazard we do not want the CLI to enable by accident.
    """
    if not commit_sha:
        raise GitHubError(
            "post_inline_review requires a head commit SHA to anchor the review"
        )
    slug = _repo_slug(repo)
    payload = {
        "commit_id": commit_sha,
        "body": body,
        "event": event,
        "comments": comments,
    }
    _gh(
        "api",
        "-X",
        "POST",
        f"repos/{slug}/pulls/{number}/reviews",
        "--input",
        "-",
        stdin=json.dumps(payload),
    )


# -- trigger words ---------------------------------------------------------

# ``!review`` forces a re-review on the next scan cycle even if a fresh report
# already exists. ``!do-not-review`` blocks all future scans — the author or a
# maintainer is opting this PR out.
#
# Matched with a trailing word boundary so the marker still fires inside prose
# ("please !review this") without firing on a word that merely starts with it:
# "ask !reviewer to look" is a sentence about a person, not a command.
_TRIGGER_REVIEW = "!review"
_TRIGGER_SKIP = "!do-not-review"
_REVIEW_RE = re.compile(r"!review\b", re.IGNORECASE)
_SKIP_RE = re.compile(r"!do-not-review\b", re.IGNORECASE)


def _pr_issue_comments(number: int, repo: str | None = None) -> list[dict]:
    slug = _repo_slug(repo)
    raw = _gh(
        "api",
        f"repos/{slug}/issues/{number}/comments",
        "--paginate",
        "--jq",
        ".[] | {id: .id, body: .body, created_at: .created_at, user_login: .user.login}",
    )
    out: list[dict] = []
    for line in raw.splitlines():
        if line.strip():
            out.append(json.loads(line))
    return out


def trigger_marker(
    comments: list[dict], since_iso: str | None = None
) -> str | None:
    """Return the most recent trigger marker in ``comments``, or ``None``.

    ``!do-not-review`` always wins when it appears at all — an opt-out is a
    property of the PR, not a state to be flipped back and forth. Otherwise
    the newest ``!review`` newer than ``since_iso`` wins. Comments older than
    ``since_iso`` are ignored (they were already acted on).
    """
    seen_review: str | None = None
    for item in comments:
        body = item.get("body") or ""
        if _SKIP_RE.search(body):
            return _TRIGGER_SKIP
        if _REVIEW_RE.search(body) and is_after(item.get("created_at"), since_iso):
            seen_review = _TRIGGER_REVIEW
    return seen_review


def pr_trigger(
    number: int, repo: str | None = None, since_iso: str | None = None
) -> str | None:
    return trigger_marker(_pr_issue_comments(number, repo), since_iso=since_iso)


# -- scan discovery --------------------------------------------------------


def list_open_prs(repo: str, target_branches: list[str] | None = None) -> list[dict]:
    """Enumerate open PRs, optionally filtered to a set of base-branch globs.

    fnmatch patterns cover the common shapes ("main", "release/*", "7.*"),
    which is what the reference implementation's settings.json uses.
    """
    import fnmatch

    raw = _gh(
        "pr",
        "list",
        "--repo",
        repo,
        "--state",
        "open",
        "--json",
        "number,title,baseRefName,headRefName,headRefOid,updatedAt,url",
        "--limit",
        "200",
    )
    prs = json.loads(raw or "[]")
    if not target_branches:
        return prs
    return [
        pr
        for pr in prs
        if any(fnmatch.fnmatch(pr.get("baseRefName") or "", pat) for pat in target_branches)
    ]
