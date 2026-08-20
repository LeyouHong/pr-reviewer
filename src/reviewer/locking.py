"""A single-holder lock for work that must not run concurrently with itself.

Cron overlap is the case this exists for: the sweep from ten minutes ago is
still running when the next one fires, both decide the same pull request needs
a review, and both post one. The window is wide — the decision is made before
a review that takes minutes, and the evidence it reads (an existing report)
only appears after that review finishes.

Scope of the guarantee, stated plainly because the previous version of this
comment overstated it: this is a lock on one filesystem, held by one process.
It stops a second invocation on the same host. It does not coordinate two
machines pointed at the same repository — that needs a claim the remote side
can see, and nothing here provides one.

Staleness is decided by age, not by liveness. A holder killed with SIGKILL
leaves the file behind, so a lock older than ``ttl_s`` is taken over rather
than honoured forever. Set the TTL above the longest sweep you expect.
"""

from __future__ import annotations

import json
import logging
import os
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

log = logging.getLogger(__name__)

DEFAULT_TTL_S = 4 * 3600


class LockHeld(RuntimeError):
    """Another holder owns the lock and it has not gone stale."""


def _read(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _age_of(path: Path) -> float:
    """Seconds since the lock was taken, from its contents then its mtime.

    The recorded timestamp is preferred because a file copied or touched by
    something else keeps a misleading mtime; mtime is the fallback for a lock
    written by an older version, or truncated by a crash mid-write.
    """
    payload = _read(path)
    taken = payload.get("taken_at")
    if isinstance(taken, (int, float)):
        return max(time.time() - float(taken), 0.0)
    try:
        return max(time.time() - path.stat().st_mtime, 0.0)
    except OSError:
        return 0.0


@contextmanager
def exclusive(path: Path, *, ttl_s: float = DEFAULT_TTL_S, label: str = "") -> Iterator[None]:
    """Hold ``path`` as a lock for the duration of the block.

    Raises :class:`LockHeld` when a live holder already owns it. Releases on
    the way out whether the body succeeded or raised, but only when this
    process is still the recorded owner — a sweep that outran the TTL must not
    delete the lock its successor now holds.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(
        {"pid": os.getpid(), "taken_at": time.time(), "label": label}
    )

    def _take() -> bool:
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except FileExistsError:
            return False
        with os.fdopen(fd, "w") as handle:
            handle.write(payload)
        return True

    if not _take():
        age = _age_of(path)
        if age < ttl_s:
            holder = _read(path)
            raise LockHeld(
                f"{path} is held by pid {holder.get('pid', '?')} "
                f"({age:.0f}s old, ttl {ttl_s:.0f}s)"
            )
        log.warning("taking over lock %s abandoned %.0fs ago", path, age)
        # Not atomic against another taker-over, and deliberately so: two
        # processes racing here have both already waited out the TTL, which
        # means the previous holder is long gone and a rare double-run is a
        # better outcome than a lock nobody can ever reclaim.
        path.unlink(missing_ok=True)
        if not _take():
            raise LockHeld(f"{path} was reclaimed by another process")

    mine = os.getpid()
    try:
        yield
    finally:
        if _read(path).get("pid") == mine:
            path.unlink(missing_ok=True)


__all__ = ["DEFAULT_TTL_S", "LockHeld", "exclusive"]
