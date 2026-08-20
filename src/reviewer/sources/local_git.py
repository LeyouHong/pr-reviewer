"""Local ``git diff`` source: review a working branch with no VCS API."""

from __future__ import annotations

import subprocess
from pathlib import Path

from ..diffing.parser import parse_unified_diff
from ..models import CodeChangeInfo


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout


def load_local_diff(repo: Path, base: str) -> CodeChangeInfo:
    """Build a review request from ``git diff base...HEAD``."""
    branch = _git(repo, "rev-parse", "--abbrev-ref", "HEAD").strip()
    try:
        merge_base = _git(repo, "merge-base", base, "HEAD").strip()
    except RuntimeError:
        merge_base = base
    diff = _git(repo, "diff", "--no-color", "-U8", f"{merge_base}..HEAD")
    subject = _git(repo, "log", "-1", "--pretty=%s").strip()
    body = _git(repo, "log", "-1", "--pretty=%b").strip()

    return CodeChangeInfo(
        repository=repo.name,
        cc_id=f"local:{branch}",
        cc_title=subject or f"Changes on {branch}",
        cc_description=body,
        source_branch=branch,
        target_branch=base,
        head_sha=_git(repo, "rev-parse", "HEAD").strip(),
        changes=parse_unified_diff(diff),
    )
