"""Stage 3: turn verdicts into the comment set that gets posted."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from ..models import ReviewComment, Severity, ValidateVerdict

log = logging.getLogger(__name__)


@dataclass
class ValidationOutcome:
    retained: list[ReviewComment] = field(default_factory=list)
    removed: list[ReviewComment] = field(default_factory=list)
    downgraded: list[ReviewComment] = field(default_factory=list)


def apply_verdict(
    comment: ReviewComment, verdict: ValidateVerdict, outcome: ValidationOutcome
) -> None:
    if verdict in (ValidateVerdict.FALSE_POSITIVE, ValidateVerdict.FALSE_POSITIVE_OOS):
        outcome.removed.append(comment)
        return

    if verdict is ValidateVerdict.TRUE_POSITIVE_SEVERITY_INFO:
        comment.severity = Severity.INFO
        outcome.downgraded.append(comment)
        outcome.retained.append(comment)
        return

    # TRUE_POSITIVE and INDETERMINATE both survive: when the pipeline cannot
    # decide, the developer is a better judge than silence.
    outcome.retained.append(comment)
