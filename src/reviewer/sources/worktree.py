"""Detached checkouts of one repository at many commits.

Anything that reads real files while judging a diff — the agentic reviewer, the
deep validator, semgrep — has to read the files *that diff was written against*.
Pointed at a working tree that has moved on, a validator marks a true positive
false because the surrounding code was since rewritten, and does so with
complete confidence. Two callers need this: the benchmark, pinning each pull
request to the commit its labels describe, and the scanner, pinning each open
pull request to its current head.

Worktrees share the origin repository's object store, so ten of them cost
inodes, not gigabytes.
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path

log = logging.getLogger(__name__)


class WorktreeError(RuntimeError):
    pass


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=False
    )
    if result.returncode != 0:
        raise WorktreeError(f"git {' '.join(args)}: {result.stderr.strip()}")
    return result.stdout


def _slug(name: str) -> str:
    """Filesystem-safe directory name for a subject like ``owner/repo#42``."""
    return name.replace("/", "_").replace("#", "-")


class WorktreePool:
    """Checkouts of one repository at many commits, cleaned up together."""

    def __init__(self, repo: Path, root: Path):
        self.repo = Path(repo).resolve()
        self.root = Path(root).resolve()
        self._made: list[Path] = []
        if not (self.repo / ".git").exists():
            raise WorktreeError(f"not a git repository: {self.repo}")

    def fetch(self, refspec: str) -> bool:
        """Fetch ``refspec`` from origin. False when the fetch fails.

        A pull request head is often absent from a plain clone, so the caller
        fetches before checking out rather than treating "not present" as
        "cannot review".
        """
        try:
            _git(self.repo, "fetch", "--quiet", "origin", refspec)
            return True
        except WorktreeError as exc:
            log.warning("could not fetch %s: %s", refspec, exc)
            return False

    def has(self, commit: str) -> bool:
        try:
            _git(self.repo, "cat-file", "-e", commit)
            return True
        except WorktreeError:
            return False

    def checkout(self, name: str, commit: str) -> Path:
        """Return a path holding ``commit``, creating the worktree if needed."""
        if not commit:
            raise WorktreeError(f"{name}: no commit to check out")
        if not self.has(commit):
            raise WorktreeError(
                f"{name}: commit {commit[:8]} is not in {self.repo}. Fetch it first, "
                "e.g. git fetch origin '+refs/pull/*/head:refs/remotes/origin/pr/*'"
            )

        target = self.root / _slug(name)
        if target.exists():
            return target
        self.root.mkdir(parents=True, exist_ok=True)
        _git(self.repo, "worktree", "add", "--detach", "--quiet", str(target), commit)
        self._made.append(target)
        log.info("checked out %s at %s", name, commit[:8])
        return target

    def release(self, name: str) -> None:
        """Drop one checkout as soon as its subject is done with it.

        A sweep over a repository with hundreds of open pull requests would
        otherwise hold every worktree it created until the sweep ends.
        """
        target = self.root / _slug(name)
        if target not in self._made:
            return
        try:
            _git(self.repo, "worktree", "remove", "--force", str(target))
        except WorktreeError as exc:
            log.warning("could not remove worktree %s: %s", target, exc)
            return
        self._made.remove(target)

    def cleanup(self) -> None:
        for path in self._made:
            try:
                _git(self.repo, "worktree", "remove", "--force", str(path))
            except WorktreeError as exc:
                log.warning("could not remove worktree %s: %s", path, exc)
        self._made.clear()

    def __enter__(self) -> "WorktreePool":
        return self

    def __exit__(self, *exc) -> None:
        self.cleanup()
