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


class DegradedCall(RuntimeError):
    """Retrying stopped, and the caller is expected to carry on without it.

    Raised in place of the original exception so a long-running loop can tell
    "this subject is not going to work, skip it" apart from "the run is over".
    Both arrive as an exception because the call has no value to return; only
    the type distinguishes them, which is what a loop needs to catch one and
    not the other. The original is attached as ``__cause__`` and repeated in
    ``original`` so a handler does not have to walk the chain.
    """

    def __init__(self, original: BaseException, *, kind: "ErrorKind", subject: str, attempts: int):
        super().__init__(
            f"{subject}: degraded after {attempts} {kind.value} failure(s): {original}"
        )
        self.original = original
        self.kind = kind
        self.subject = subject
        self.attempts = attempts


@dataclass(frozen=True)
class Verdict:
    """Classifier output: the kind plus a short evidence tag for logs."""

    kind: ErrorKind
    signal: str = ""
