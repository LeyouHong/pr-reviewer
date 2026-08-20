"""Point the file-reading stages at the revision they are judging.

The agentic reviewer, the deep validator, and semgrep all open real files. Aim
them at a tree that has moved on and they do not fail — they return confident
conclusions about code the diff never touched. A true positive whose
surroundings were since rewritten reads as a false positive, and the reverse.

So a review gets a worktree detached at its own head commit, and when no clone
is configured for that repository the stages that would read files are switched
off instead of falling back to whatever directory the process started in. A
diff-only review is a smaller answer; it is still an answer about the right
code.

One implementation for both callers. The scanner and the queue worker need
exactly this, and two copies would drift until one of them was quietly reading
the wrong tree again.
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from typing import Iterator

from ..config import Config
from .worktree import WorktreeError, WorktreePool

log = logging.getLogger(__name__)


class CheckoutProvider:
    """Local clones, keyed by repository, plus the worktrees cut from them."""

    def __init__(self, checkouts: dict[str, Path] | None = None):
        self._checkouts = dict(checkouts or {})
        self._pools: dict[str, WorktreePool] = {}

    def clone_for(self, repo: str) -> Path | None:
        return self._checkouts.get(repo)

    def _pool(self, repo: str) -> WorktreePool | None:
        if repo in self._pools:
            return self._pools[repo]
        clone = self._checkouts.get(repo)
        if clone is None:
            return None
        try:
            pool = WorktreePool(clone, clone.parent / ".pr-reviewer-worktrees")
        except WorktreeError as exc:
            log.error("checkout: %s declares %s but %s", repo, clone, exc)
            return None
        self._pools[repo] = pool
        return pool

    @contextmanager
    def pinned(
        self, config: Config, repo: str, number: int, head_sha: str
    ) -> Iterator[Config]:
        """Yield a config whose ``repo_path`` holds ``head_sha``.

        Degrades rather than guesses: an unconfigured repository, a missing
        commit, or a failed worktree all yield a config with the file-reading
        stages disabled, and say why.
        """
        pool = self._pool(repo)
        if pool is None:
            if config.enable_validation or config.agentic_review:
                log.warning(
                    "checkout: no clone configured for %s; validation and agentic "
                    "review are off so no stage reads an unrelated tree",
                    repo,
                )
            yield replace(config, enable_validation=False, agentic_review=False)
            return

        name = f"{repo}#{number}"
        if head_sha and not pool.has(head_sha):
            pool.fetch(f"+refs/pull/{number}/head:refs/remotes/origin/pr/{number}")

        try:
            path = pool.checkout(name, head_sha)
        except WorktreeError as exc:
            log.warning(
                "checkout: cannot pin %s to %s (%s); reviewing the diff without "
                "file access",
                name,
                head_sha[:8] or "?",
                exc,
            )
            yield replace(config, enable_validation=False, agentic_review=False)
            return

        try:
            yield replace(config, repo_path=path)
        finally:
            pool.release(name)

    def cleanup(self) -> None:
        for pool in self._pools.values():
            pool.cleanup()
        self._pools.clear()

    @classmethod
    def from_settings(cls, settings) -> "CheckoutProvider":
        """Build from a ``ScanSettings``, using each repo's declared checkout."""
        return cls(
            {
                repo.url: repo.checkout
                for repo in settings.repositories
                if repo.checkout is not None
            }
        )


__all__ = ["CheckoutProvider"]
