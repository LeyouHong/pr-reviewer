"""Per-PR checkouts pinned to the commit the labels were written against.

The agentic validator reads real files. Pointing it at a working tree that has
moved on since the PR means it validates today's code against yesterday's diff,
which silently corrupts precision — a true positive whose surrounding code has
since been rewritten looks like a false positive, and vice versa.

Each PR therefore gets a detached worktree at its ``pin_commit``. Worktrees
share the origin repository's object store, so ten of them cost inodes, not
gigabytes.
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


class WorktreePool:
    """Checkouts of one repository at many commits, cleaned up together."""

    def __init__(self, repo: Path, root: Path):
        self.repo = Path(repo).resolve()
        self.root = Path(root).resolve()
        self._made: list[Path] = []
        if not (self.repo / ".git").exists():
            raise WorktreeError(f"not a git repository: {self.repo}")

    def has(self, commit: str) -> bool:
        try:
            _git(self.repo, "cat-file", "-e", commit)
            return True
        except WorktreeError:
            return False

    def checkout(self, name: str, commit: str) -> Path:
        """Return a path holding ``commit``, creating the worktree if needed."""
        if not commit:
            raise WorktreeError(f"{name}: corpus entry has no pin_commit")
        if not self.has(commit):
            raise WorktreeError(
                f"{name}: commit {commit[:8]} is not in {self.repo}. Fetch it first, "
                "e.g. git fetch origin '+refs/pull/*/head:refs/remotes/origin/pr/*'"
            )

        target = self.root / name.replace("/", "_").replace("#", "-")
        if target.exists():
            return target
        self.root.mkdir(parents=True, exist_ok=True)
        _git(self.repo, "worktree", "add", "--detach", "--quiet", str(target), commit)
        self._made.append(target)
        log.info("checked out %s at %s", name, commit[:8])
        return target

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
