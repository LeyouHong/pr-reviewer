"""Backward-compat shim.

The classification and retry machinery moved to :mod:`reviewer.exception_handling`.
This module re-exports the symbols old call sites (and tests) still import from
``reviewer.provider.errors``.
"""

from __future__ import annotations

from ..exception_handling import (
    BillingError,
    ContentError,
    DegenerateOutputError,
    ErrorKind,
    classify,
    is_billing_failure,
    with_retries,
)

__all__ = [
    "BillingError",
    "ContentError",
    "DegenerateOutputError",
    "ErrorKind",
    "classify",
    "is_billing_failure",
    "with_retries",
]
