"""Centralised error classification and backoff.

No call site rolls its own retry. Every provider failure is walked down its
``__cause__``/``__context__`` chain and mapped to one of five kinds, each with a
fixed remedy.
"""

from __future__ import annotations

import logging
import random
import time
from enum import Enum
from typing import Callable, TypeVar

from .. import constants

log = logging.getLogger(__name__)

T = TypeVar("T")

_MAX_CHAIN_DEPTH = 5


class ErrorKind(str, Enum):
    DEGENERATE = "degenerate"
    TRANSPORT = "transport"
    CAPACITY = "capacity"
    CONTENT = "content"
    FATAL = "fatal"


class DegenerateOutputError(RuntimeError):
    """The model returned empty, truncated, or collapsed output.

    DeepSeek's own docs warn that JSON output "may occasionally return empty
    content", so this is a routine retry, never a fatal.
    """


class ContentError(RuntimeError):
    """Output did not satisfy the schema and re-driving the model may fix it."""


class BillingError(RuntimeError):
    """The account cannot pay for the request.

    Distinct from a rate limit even though both arrive as a refusal to serve:
    waiting fixes a rate limit and does nothing at all for an empty wallet.
    Classified FATAL so it fails on the first attempt, and surfaced as its own
    type so a long run can abort instead of grinding every remaining unit of
    work against the same wall.
    """ 


_TRANSPORT_MARKERS = (
    "timed out",
    "timeout",
    "cannot connect",
    "connection reset",
    "connection error",
    "bad gateway",
    "service unavailable",
    "internal server error",
    "502",
    "503",
    "504",
)

_CAPACITY_MARKERS = (
    "rate limit",
    "rate_limit",
    "too many requests",
    "429",
    "concurrency",
)

# Checked before the capacity markers: these never clear on their own.
_BILLING_MARKERS = (
    "insufficient balance",
    "insufficient_quota",
    "exceeded your current quota",
    "billing",
    "payment required",
    " 402",
    "402 -",
)

_CONTENT_MARKERS = (
    "invalid_request_error",
    "json",
    "schema",
    "too_long",
    "maximum context length",
)


def _chain(exc: BaseException):
    seen = 0
    current: BaseException | None = exc
    while current is not None and seen < _MAX_CHAIN_DEPTH:
        yield current
        current = current.__cause__ or current.__context__
        seen += 1


def classify(exc: BaseException) -> ErrorKind:
    for link in _chain(exc):
        if isinstance(link, DegenerateOutputError):
            return ErrorKind.DEGENERATE
        if isinstance(link, ContentError):
            return ErrorKind.CONTENT

        if isinstance(link, BillingError):
            return ErrorKind.FATAL

        name = type(link).__name__.lower()
        text = str(link).lower()

        # Must precede the capacity check: an unpayable account is not a queue
        # to wait in, and retrying it burns the backoff budget for nothing.
        if any(m in text for m in _BILLING_MARKERS):
            return ErrorKind.FATAL

        if "ratelimit" in name or any(m in text for m in _CAPACITY_MARKERS):
            return ErrorKind.CAPACITY
        if "timeout" in name or "connection" in name or "apiconnection" in name:
            return ErrorKind.TRANSPORT
        if any(m in text for m in _TRANSPORT_MARKERS):
            return ErrorKind.TRANSPORT
        if "validationerror" in name:
            return ErrorKind.CONTENT
        if "badrequest" in name and any(m in text for m in _CONTENT_MARKERS):
            return ErrorKind.CONTENT

    return ErrorKind.FATAL


_BUDGETS = {
    ErrorKind.DEGENERATE: constants.MAX_DEGENERATE_RETRIES,
    ErrorKind.TRANSPORT: constants.MAX_TRANSPORT_RETRIES,
    ErrorKind.CAPACITY: constants.MAX_CAPACITY_RETRIES,
    ErrorKind.CONTENT: constants.MAX_PARSE_RETRIES,
    ErrorKind.FATAL: 0,
}


def _delay(kind: ErrorKind, attempt: int) -> float:
    if kind is ErrorKind.CAPACITY:
        return constants.CAPACITY_BACKOFF_S
    raw = constants.BACKOFF_BASE_S * (2 ** max(attempt - 1, 0))
    return min(raw, constants.BACKOFF_CAP_S) + random.uniform(0, 1.0)


def with_retries(fn: Callable[[], T], *, label: str = "call") -> T:
    """Run ``fn``, retrying per the error-kind budget.

    Budgets are tracked per kind, so a run that alternates between a transient
    timeout and a schema flake does not silently exhaust a single shared
    counter.
    """
    used: dict[ErrorKind, int] = {}
    last: BaseException | None = None

    while True:
        try:
            return fn()
        except BaseException as exc:  # noqa: BLE001 - classified below
            kind = classify(exc)
            used[kind] = used.get(kind, 0) + 1
            last = exc

            if used[kind] > _BUDGETS[kind]:
                log.error(
                    "%s: giving up after %d %s failures: %s",
                    label,
                    used[kind],
                    kind.value,
                    exc,
                )
                raise

            wait = _delay(kind, used[kind])
            log.warning(
                "%s: %s failure %d/%d, retrying in %.1fs: %s",
                label,
                kind.value,
                used[kind],
                _BUDGETS[kind],
                wait,
                exc,
            )
            time.sleep(wait)

    raise AssertionError(f"unreachable: {last}")


def is_billing_failure(exc: BaseException) -> bool:
    """True when the failure is an unpayable account rather than a bad request."""
    for link in _chain(exc):
        if isinstance(link, BillingError):
            return True
        if any(m in str(link).lower() for m in _BILLING_MARKERS):
            return True
    return False
