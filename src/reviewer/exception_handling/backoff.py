"""Backoff curves.

Deterministic (no jitter) so unit-testable; callers add jitter on top when they
need it. Two shapes cover every retry in the pipeline: exponential growth for
transient network/degenerate failures, and a fixed ceiling for capacity waits
where doubling would be counterproductive.
"""

from __future__ import annotations

import enum


class BackoffKind(enum.Enum):
    FIXED = "fixed"
    EXPONENTIAL = "exponential"


def exponential_backoff(
    attempt: int,
    *,
    base: float,
    cap: float,
    first_attempt: int = 1,
) -> float:
    """``base * 2 ** (attempt - first_attempt)``, capped at ``cap``.

    Exponent is floored at 0 so the first retry waits ``base`` seconds, not
    ``base / 2``.
    """
    exponent = max(attempt - first_attempt, 0)
    return min(base * (2 ** exponent), cap)


def fixed_backoff(wait_s: float) -> float:
    """Return the fixed wait unchanged.

    Exists as a helper so call sites read symmetrically with
    :func:`exponential_backoff` — the choice of curve is the interesting bit.
    """
    return wait_s
