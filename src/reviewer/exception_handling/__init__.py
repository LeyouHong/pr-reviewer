"""Centralised error classification, backoff, and retry policies.

One entry point (``with_retries``) covers every provider call in the pipeline,
so no call site rolls its own retry. Long-running loops (Batch 3 cron / Sleep)
can swap in :class:`NeverTerminatePolicy` without touching the retry mechanism.
"""

from __future__ import annotations

from .backoff import BackoffKind, exponential_backoff, fixed_backoff
from .policy import (
    BoundedRetryPolicy,
    NeverTerminatePolicy,
    Outcome,
    OutcomeKind,
    RetryPolicy,
    with_retries,
)
from .routing import classify, is_billing_failure
from .types import (
    BillingError,
    ContentError,
    DegenerateOutputError,
    ErrorKind,
    Verdict,
)

__all__ = [
    "BackoffKind",
    "BillingError",
    "BoundedRetryPolicy",
    "ContentError",
    "DegenerateOutputError",
    "ErrorKind",
    "NeverTerminatePolicy",
    "Outcome",
    "OutcomeKind",
    "RetryPolicy",
    "Verdict",
    "classify",
    "exponential_backoff",
    "fixed_backoff",
    "is_billing_failure",
    "with_retries",
]
