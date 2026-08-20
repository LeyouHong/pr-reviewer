"""Centralised error classification, backoff, and retry policies.

One entry point (``with_retries``) covers every provider call in the pipeline,
so no call site rolls its own retry. Long-running loops (Batch 3 cron / Sleep)
can swap in :class:`NeverTerminatePolicy` without touching the retry mechanism.
"""

from __future__ import annotations

from .backoff import BackoffKind, exponential_backoff, fixed_backoff, with_jitter
from .policy import (
    BoundedRetryPolicy,
    NeverTerminatePolicy,
    Outcome,
    OutcomeKind,
    RetryPolicy,
    with_retries,
)
from .routing import classify, is_billing_failure, looks_like_usage_limit
from .types import (
    DegradedCall,
    UsageLimitError,
    BillingError,
    ContentError,
    DegenerateOutputError,
    ErrorKind,
    Verdict,
)

__all__ = [
    "BackoffKind",
    "with_jitter",
    "BillingError",
    "BoundedRetryPolicy",
    "ContentError",
    "DegenerateOutputError",
    "ErrorKind",
    "DegradedCall",
    "UsageLimitError",
    "looks_like_usage_limit",
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
