"""Error kinds, verdict record, and the exception types the classifier reads.

Kept dependency-light so every layer above can import these without cycles.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


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


@dataclass(frozen=True)
class Verdict:
    """Classifier output: the kind plus a short evidence tag for logs."""

    kind: ErrorKind
    signal: str = ""
