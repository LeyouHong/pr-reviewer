"""Backoff curves.

The curves are deterministic so they can be unit-tested; :func:`with_jitter`
is the separate, explicit step that spreads them. Two shapes cover every retry
in the pipeline: exponential growth for transient network/degenerate failures,
and a fixed ceiling for capacity waits where doubling would be
counterproductive.
"""

from __future__ import annotations

import enum
import random


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


def with_jitter(wait_s: float, *, spread: float = 0.5) -> float:
    """Scatter ``wait_s`` within ``[wait_s * (1 - spread), wait_s]``.

    Proportional rather than additive, because these waits span three orders of
    magnitude: a one-second nudge on a 3600-second backoff leaves a fleet of
    workers as synchronised as it found them, which is exactly the case a long
    outage produces. Never returns more than ``wait_s``, so a caller's cap
    stays a cap.
    """
    spread = min(max(spread, 0.0), 1.0)
    return wait_s * (1.0 - spread * random.random())
