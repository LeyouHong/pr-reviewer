"""A durable job queue that survives the process holding it.

A webhook must answer in seconds and a review takes minutes, so the two cannot
be the same operation. What sits between them has to outlive a crash: a job
accepted and then lost is a review that silently never happens, which is the
failure this whole design exists to prevent.

Directories and atomic renames rather than a database, because the properties
that matter here are all filesystem properties. ``os.replace`` is atomic, so
two workers cannot claim the same job; a job file is a readable JSON document,
so an operator can see the backlog with ``ls``; and there is no schema to
migrate when the job shape changes.

    pending/   accepted, waiting for a worker
    claimed/   a worker is on it, with the claim time in the name
    done/      finished; kept as the audit trail
    failed/    gave up, with the error recorded

Identity is ``(repo, number, head_sha)``. A revision is reviewed once no matter
how many times it is offered — by a webhook redelivery, by the reconciling
sweep, or by a worker retrying after a crash.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)

_LANES = ("pending", "claimed", "done", "failed")

# A claim older than this is assumed to belong to a dead worker. Set it above
# the longest review you expect: reclaiming a job that is merely slow means two
# workers review the same revision, and only the posted-report check downstream
# stops that becoming two comments.
DEFAULT_CLAIM_TTL_S = 3600.0

_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")


@dataclass
class Job:
    """One revision of one pull request, waiting to be reviewed."""

    repo: str
    number: int
    head_sha: str
    reason: str = "webhook"
    enqueued_at: float = field(default_factory=time.time)
    attempts: int = 0
    error: str = ""

    @property
    def key(self) -> str:
        """Identity, and the filename. Two offers of one revision collide here."""
        return f"{_UNSAFE.sub('-', self.repo)}--{self.number}--{self.head_sha[:12]}"

    @property
    def label(self) -> str:
        return f"{self.repo}#{self.number}@{self.head_sha[:8]}"


class JobQueue:
    def __init__(self, root: Path, *, claim_ttl_s: float = DEFAULT_CLAIM_TTL_S):
        self.root = Path(root)
        self.claim_ttl_s = claim_ttl_s
        for lane in _LANES:
            (self.root / lane).mkdir(parents=True, exist_ok=True)

    # -- paths ------------------------------------------------------------

    def _lane(self, lane: str) -> Path:
        return self.root / lane

    def _seen(self, key: str) -> bool:
        """True when this revision is already queued, running, or finished.

        ``failed`` is deliberately not consulted: a job that failed should be
        retryable by re-offering it, and the operator who cleared the failure
        should not have to also delete a tombstone.
        """
        return any(
            (self._lane(lane) / f"{key}.json").exists()
            for lane in ("pending", "done")
        ) or bool(list(self._lane("claimed").glob(f"*--{key}.json")))

    # -- producer ---------------------------------------------------------

    def enqueue(self, job: Job) -> bool:
        """Accept ``job`` unless this revision is already accounted for.

        Returns whether it was newly accepted, so a caller can log the
        difference between "queued" and "already known" instead of guessing.
        """
        if self._seen(job.key):
            log.info("queue: %s already known, not re-queued", job.label)
            return False

        superseded = self.supersede(job)
        if superseded:
            log.info("queue: %s supersedes %d earlier revision(s)", job.label, superseded)

        path = self._lane("pending") / f"{job.key}.json"
        _write_atomic(path, asdict(job))
        log.info("queue: accepted %s (%s)", job.label, job.reason)
        return True

    def supersede(self, job: Job) -> int:
        """Drop pending jobs for the same pull request at an older revision.

        Three pushes in a minute should cost one review of the newest revision,
        not three reviews of which two are already wrong. Only *pending* jobs
        are dropped — a claimed job is mid-flight and cancelling it from here
        would leave a worker writing results for a job that no longer exists.
        """
        prefix = f"{_UNSAFE.sub('-', job.repo)}--{job.number}--"
        dropped = 0
        for path in self._lane("pending").glob(f"{prefix}*.json"):
            if path.name == f"{job.key}.json":
                continue
            path.unlink(missing_ok=True)
            dropped += 1
        return dropped

    # -- consumer ---------------------------------------------------------

    def claim(self) -> tuple[Job, Path] | None:
        """Take the oldest pending job, or reclaim one abandoned by a dead worker.

        The claim is an atomic rename, so two workers racing for the same job
        produce one winner and one ``FileNotFoundError`` that simply moves on.
        """
        self._reclaim_stale()
        candidates = sorted(
            self._lane("pending").glob("*.json"), key=lambda p: p.stat().st_mtime
        )
        for path in candidates:
            target = self._lane("claimed") / f"{int(time.time())}--{path.name}"
            try:
                os.replace(path, target)
            except FileNotFoundError:
                continue  # another worker won it
            payload = json.loads(target.read_text(encoding="utf-8"))
            return Job(**payload), target
        return None

    def _reclaim_stale(self) -> None:
        cutoff = time.time() - self.claim_ttl_s
        for path in self._lane("claimed").glob("*.json"):
            stamp, _, original = path.name.partition("--")
            try:
                claimed_at = float(stamp)
            except ValueError:
                continue
            if claimed_at > cutoff:
                continue
            log.warning(
                "queue: reclaiming %s, claimed %.0fs ago", original, time.time() - claimed_at
            )
            try:
                os.replace(path, self._lane("pending") / original)
            except OSError as exc:
                log.error("queue: could not reclaim %s: %s", original, exc)

    def complete(self, job: Job, claimed: Path) -> None:
        _write_atomic(self._lane("done") / f"{job.key}.json", asdict(job))
        claimed.unlink(missing_ok=True)
        log.info("queue: completed %s", job.label)

    def fail(self, job: Job, claimed: Path, error: str) -> None:
        job.error = error
        _write_atomic(self._lane("failed") / f"{job.key}.json", asdict(job))
        claimed.unlink(missing_ok=True)
        log.error("queue: failed %s: %s", job.label, error[:200])

    def requeue(self, job: Job, claimed: Path) -> None:
        """Put a job back for another attempt, counting the try."""
        job.attempts += 1
        _write_atomic(self._lane("pending") / f"{job.key}.json", asdict(job))
        claimed.unlink(missing_ok=True)
        log.warning("queue: requeued %s (attempt %d)", job.label, job.attempts)

    # -- observability ----------------------------------------------------

    def depth(self) -> dict[str, int]:
        return {lane: len(list(self._lane(lane).glob("*.json"))) for lane in _LANES}


def _write_atomic(path: Path, payload: dict) -> None:
    """Write via a temporary file and rename, so no reader sees a partial job."""
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    os.replace(tmp, path)


__all__ = ["DEFAULT_CLAIM_TTL_S", "Job", "JobQueue"]
