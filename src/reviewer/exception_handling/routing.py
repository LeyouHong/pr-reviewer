"""Classify a failure into an :class:`ErrorKind`.

Walks the ``__cause__`` / ``__context__`` chain (depth ≤ 5) so a bare wrapper
around a real transport error does not fall through to FATAL.
"""

from __future__ import annotations

from typing import Iterator

from .types import (
    BillingError,
    ContentError,
    DegenerateOutputError,
    ErrorKind,
)

_MAX_CHAIN_DEPTH = 5

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


def _chain(exc: BaseException) -> Iterator[BaseException]:
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


def is_billing_failure(exc: BaseException) -> bool:
    """True when the failure is an unpayable account rather than a bad request."""
    for link in _chain(exc):
        if isinstance(link, BillingError):
            return True
        if any(m in str(link).lower() for m in _BILLING_MARKERS):
            return True
    return False
