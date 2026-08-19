"""GitHub PR source, driven through the ``gh`` CLI.

Using ``gh`` rather than raw REST means the tool inherits whatever auth the
developer already has, and there is no token to store or refresh.
"""

from __future__ import annotations

import json
import logging
import subprocess
from datetime import datetime, timezone

from ..constants import REPORT_FINGERPRINT
from ..diffing.parser import parse_unified_diff
from ..models import CodeChangeInfo

log = logging.getLogger(__name__)


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
    cutoff = datetime.fromisoformat(since_iso.replace("Z", "+00:00"))
    for item in existing_report_comments(number, repo):
        created = datetime.fromisoformat(item["created_at"].replace("Z", "+00:00"))
        if created.astimezone(timezone.utc) > cutoff.astimezone(timezone.utc):
            return True
    return False


def prune_old_reports(number: int, keep: int, repo: str | None = None) -> int:
    """Delete all but the newest ``keep`` prior reports. Returns count deleted."""
    slug = _repo_slug(repo)
    comments = existing_report_comments(number, repo)
    comments.sort(key=lambda c: c["created_at"])
    stale = comments[: max(len(comments) - keep, 0)]
    for item in stale:
        _gh("api", "-X", "DELETE", f"repos/{slug}/issues/comments/{item['id']}")
    return len(stale)


def post_report(number: int, body: str, repo: str | None = None) -> None:
    scope = ["--repo", repo] if repo else []
    _gh("pr", "comment", str(number), *scope, "--body-file", "-", stdin=body)
