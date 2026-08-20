"""Retry policies and the single ``with_retries`` entrypoint.

Two contracts, kept side-by-side so the difference is a visible diff rather than
buried in two feature loops:

* :class:`BoundedRetryPolicy` — the default for one-shot requests. Per-kind
  budgets; when a kind exhausts its budget the caller sees the original
  exception. Preserves the behaviour of the pre-refactor ``with_retries``.
* :class:`NeverTerminatePolicy` — for long-running loops (Batch 3 cron / Sleep):
  transient failures retry unbounded with exponential backoff, but an opaque 5xx
  or degenerate completion recurring on one subject is treated as item-poison
  after a small ceiling and reported as ``DEGRADE`` instead of pinning the
  worker forever.

The policies are pure decision objects — they own attempt counters and return
an :class:`Outcome`; they do not sleep or mutate global state. ``with_retries``
is the thin wrapper that actually sleeps between attempts.
"""

from __future__ import annotations

import abc
import enum
import logging
import time
from dataclasses import dataclass
from typing import Callable, TypeVar

from .. import constants
from .backoff import exponential_backoff, fixed_backoff, with_jitter
from .routing import classify
from .types import DegradedCall, ErrorKind, UsageLimitError

log = logging.getLogger(__name__)

T = TypeVar("T")

# A re-issue draws a new seed, so a collapse is worth another attempt.
_TRANSIENT = frozenset({ErrorKind.TRANSPORT, ErrorKind.CAPACITY, ErrorKind.DEGENERATE})


def _window_wait(exc: BaseException | None) -> float | None:
    """Seconds to wait when the failure is a subscription window, else None.

    A usage window reopens on a schedule the provider knows and we do not, so
    when it tells us, that timestamp beats every backoff curve: waiting thirty
    seconds re-hits a wall that lifts in two hours, and giving up throws away a
    sweep that would have resumed on its own. Ten seconds of slack absorbs
    clock skew, and an unknown reset falls through to the normal curve rather
    than inventing a duration.
    """
    if exc is None:
        return None
    for link in (exc, exc.__cause__, exc.__context__):
        if isinstance(link, UsageLimitError):
            remaining = link.seconds_remaining()
            if remaining is not None:
                return remaining + 10.0
    return None


class OutcomeKind(enum.Enum):
    """What the caller must do with a failed attempt."""

    RETRY = "retry"       # transient; sleep ``wait_s`` and re-invoke
    DEGRADE = "degrade"   # give up gracefully; loop continues on next subject
    TERMINAL = "terminal" # give up loudly; caller raises


@dataclass(frozen=True)
class Outcome:
    kind: OutcomeKind
    error_kind: ErrorKind
    attempt: int
    wait_s: float = 0.0


class RetryPolicy(abc.ABC):
    """Decision contract for a failed attempt.

    Concrete policies own per-subject attempt counters. ``decide`` classifies
    the exception and returns an :class:`Outcome`; ``reset`` clears a subject's
    counter on success so a later, unrelated failure starts fresh.
    """

    @abc.abstractmethod
    def decide(self, exc: BaseException, *, subject: str) -> Outcome: ...

    @abc.abstractmethod
    def reset(self, subject: str) -> None: ...


class BoundedRetryPolicy(RetryPolicy):
    """Per-kind budgeted retries; TERMINAL once a kind is exhausted.

    Preserves the pre-refactor behaviour of ``with_retries``: budgets are
    tracked per kind, so a run that alternates between a transient timeout and
    a schema flake does not silently exhaust a single shared counter.
    """

    def __init__(
        self,
        *,
        max_degenerate_retries: int = constants.MAX_DEGENERATE_RETRIES,
        max_transport_retries: int = constants.MAX_TRANSPORT_RETRIES,
        max_capacity_retries: int = constants.MAX_CAPACITY_RETRIES,
        max_content_retries: int = constants.MAX_PARSE_RETRIES,
        backoff_base_s: float = constants.BACKOFF_BASE_S,
        backoff_cap_s: float = constants.BACKOFF_CAP_S,
        capacity_backoff_s: float = constants.CAPACITY_BACKOFF_S,
    ) -> None:
        self._budgets: dict[ErrorKind, int] = {
            ErrorKind.DEGENERATE: max_degenerate_retries,
            ErrorKind.TRANSPORT: max_transport_retries,
            ErrorKind.CAPACITY: max_capacity_retries,
            ErrorKind.CONTENT: max_content_retries,
            ErrorKind.FATAL: 0,
        }
        self._base = backoff_base_s
        self._cap = backoff_cap_s
        self._capacity_wait = capacity_backoff_s
        self._used: dict[str, dict[ErrorKind, int]] = {}

    def _delay(self, kind: ErrorKind, attempt: int, exc: BaseException | None = None) -> float:
        window = _window_wait(exc)
        if window is not None:
            return window
        if kind is ErrorKind.CAPACITY:
            return with_jitter(fixed_backoff(self._capacity_wait), spread=0.25)
        raw = exponential_backoff(attempt, base=self._base, cap=self._cap)
        return with_jitter(raw, spread=0.25)

    def decide(self, exc: BaseException, *, subject: str) -> Outcome:
        kind = classify(exc)
        counters = self._used.setdefault(subject, {})
        counters[kind] = counters.get(kind, 0) + 1
        attempt = counters[kind]

        if attempt > self._budgets[kind]:
            return Outcome(OutcomeKind.TERMINAL, kind, attempt)
        return Outcome(OutcomeKind.RETRY, kind, attempt, wait_s=self._delay(kind, attempt, exc))

    def reset(self, subject: str) -> None:
        self._used.pop(subject, None)


# ``NeverTerminatePolicy`` defaults mirror the fortinac reference so a long-lived
# cron loop can adopt it without further tuning. Chosen to be larger than the
# bounded defaults on purpose: this policy is for loops that must survive a
# multi-hour endpoint outage.
_NEVER_BASE_S = 60.0
_NEVER_CAP_S = 3600.0
_NEVER_MAX_UNMATCHED_5XX = 6
_NEVER_MAX_DEGENERATE = 3


class NeverTerminatePolicy(RetryPolicy):
    """Long-running loop contract: degrade-and-continue, never TERMINAL.

    Transport / capacity / degenerate failures retry unbounded with exponential
    backoff. Two escape hatches keep a single poisoned subject from wedging the
    worker:

    * An opaque 5xx (transport with no more specific signal) recurring beyond
      ``max_unmatched_5xx_retries`` DEGRADEs that subject.
    * A completion that keeps collapsing (DEGENERATE) beyond
      ``max_degenerate_retries`` DEGRADEs likewise — every attempt costs a full
      generation, so the ceiling is deliberately small.

    Deterministic failures (FATAL / CONTENT) always DEGRADE immediately.
    """

    def __init__(
        self,
        *,
        base_backoff_s: float = _NEVER_BASE_S,
        backoff_cap_s: float = _NEVER_CAP_S,
        max_unmatched_5xx_retries: int = _NEVER_MAX_UNMATCHED_5XX,
        max_degenerate_retries: int = _NEVER_MAX_DEGENERATE,
    ) -> None:
        self._base = base_backoff_s
        self._cap = backoff_cap_s
        self._max_5xx = max_unmatched_5xx_retries
        self._max_degenerate = max_degenerate_retries
        self._attempts: dict[str, dict[ErrorKind, int]] = {}

    def decide(self, exc: BaseException, *, subject: str) -> Outcome:
        kind = classify(exc)
        counters = self._attempts.setdefault(subject, {})
        counters[kind] = counters.get(kind, 0) + 1
        attempt = counters[kind]

        if kind not in _TRANSIENT:
            return Outcome(OutcomeKind.DEGRADE, kind, attempt)

        if kind is ErrorKind.TRANSPORT and attempt > self._max_5xx:
            # A single subject repeatedly failing on transport is item-poison:
            # every remaining attempt just re-hits the same wall.
            return Outcome(OutcomeKind.DEGRADE, kind, attempt)
        if kind is ErrorKind.DEGENERATE and attempt > self._max_degenerate:
            return Outcome(OutcomeKind.DEGRADE, kind, attempt)

        window = _window_wait(exc)
        if window is not None:
            return Outcome(OutcomeKind.RETRY, kind, attempt, wait_s=window)

        # Widest spread of the two policies: this is the one that survives a
        # multi-hour outage, so every worker watching that outage comes back at
        # a different moment instead of re-creating the stampede on recovery.
        wait = with_jitter(
            exponential_backoff(attempt, base=self._base, cap=self._cap), spread=0.5
        )
        return Outcome(OutcomeKind.RETRY, kind, attempt, wait_s=wait)

    def reset(self, subject: str) -> None:
        self._attempts.pop(subject, None)


def with_retries(
    fn: Callable[[], T],
    *,
    label: str = "call",
    policy: RetryPolicy | None = None,
) -> T:
    """Invoke ``fn`` under a retry policy, sleeping between attempts.

    Default policy is :class:`BoundedRetryPolicy` with the constants configured
    for one-shot review requests, matching pre-refactor behaviour. Long-running
    loops pass their own :class:`NeverTerminatePolicy`.

    Two failure exits, and the type is what tells them apart:

    * TERMINAL re-raises the original exception. The caller asked for a value
      and there is none; whatever handles provider errors handles this.
    * DEGRADE raises :class:`DegradedCall`. The subject is not going to work
      and the loop should move to the next one, so a sweep catches
      ``DegradedCall`` and continues while still letting a genuine fault out.
    """
    active = policy or BoundedRetryPolicy()

    while True:
        try:
            result = fn()
            active.reset(label)
            return result
        except BaseException as exc:  # noqa: BLE001 - classified by the policy
            outcome = active.decide(exc, subject=label)
            if outcome.kind is OutcomeKind.TERMINAL:
                log.error(
                    "%s: giving up after %d %s failures: %s",
                    label,
                    outcome.attempt,
                    outcome.error_kind.value,
                    exc,
                )
                raise
            if outcome.kind is OutcomeKind.DEGRADE:
                log.error(
                    "%s: degrading after %d %s failures: %s",
                    label,
                    outcome.attempt,
                    outcome.error_kind.value,
                    exc,
                )
                raise DegradedCall(
                    exc,
                    kind=outcome.error_kind,
                    subject=label,
                    attempts=outcome.attempt,
                ) from exc
            log.warning(
                "%s: %s failure %d, retrying in %.1fs: %s",
                label,
                outcome.error_kind.value,
                outcome.attempt,
                outcome.wait_s,
                exc,
            )
            time.sleep(outcome.wait_s)
