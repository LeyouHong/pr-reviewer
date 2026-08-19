"""Stage 1: the qualification gate.

Cheap regex heuristics run first and settle the clear cases; the LLM is asked
only about what survives. The tie-break is deliberately asymmetric: when torn
between ``pass`` and ``validate``, choose ``validate``. A wrong ``pass`` lands a
false positive in front of a developer, while a wrong ``validate`` costs one
extra call.
"""

from __future__ import annotations

import logging
import re

from ..models import (
    FileChange,
    QualifyVerdict,
    ReviewComment,
    Severity,
)
from ..diffing.scope import scope_section
from ..prompt import PromptLibrary, render
from ..provider import DeepSeekClient

log = logging.getLogger(__name__)

# Four narrow patterns, not one broad "correct" match. Broader matches cause
# false discards, and a discard is silent — the comment never reaches anyone.
# To widen coverage, add another narrow pattern; do not loosen these.
_BUGFIX_AS_ERROR_PATTERNS = (
    re.compile(r"\bthe fix\b.{0,30}\bcorrect(ly)?\b", re.IGNORECASE),
    re.compile(
        r"\bcorrectly\s+(handles?|fixes|addresses|resolves?|implements?)\b",
        re.IGNORECASE,
    ),
    re.compile(r"\bfix\s+is\s+(correct|proper|appropriate|valid)\b", re.IGNORECASE),
    re.compile(r"\bproperly\s+(handles?|fixes|addresses|resolves?)\b", re.IGNORECASE),
)

_CONTRASTIVE_PATTERN = re.compile(
    r"\b(but|however|although|yet|nevertheless|still|whereas)\b", re.IGNORECASE
)

# Uncertainty needs a verb complement. Bare "may" appears in solid findings
# ("this may be a security issue if the token is guessable") and must not by
# itself force an expensive validation pass.
_UNCERTAINTY_PATTERNS = (
    re.compile(
        r"\b(may|might|could)\s+(cause|lead|result|introduce|break)\b", re.IGNORECASE
    ),
    re.compile(r"\bpotentially\b", re.IGNORECASE),
    re.compile(r"\bappears?\s+to\b", re.IGNORECASE),
    re.compile(r"\bit\s+seems?\b", re.IGNORECASE),
    re.compile(r"\bcheck\s+whether\b", re.IGNORECASE),
)

# Noun phrases: the reviewer *claiming* cross-file impact, which is exactly
# what deep validation exists to verify.
_CROSS_FILE_PATTERNS = (
    re.compile(r"\bin\s+other\s+files?\b", re.IGNORECASE),
    re.compile(r"\bcallers?\s+of\b", re.IGNORECASE),
    re.compile(r"\bdepends?\s+on\b", re.IGNORECASE),
    re.compile(r"\bcall\s+sites?\b", re.IGNORECASE),
    re.compile(r"\bused\s+(by|in)\s+other\b", re.IGNORECASE),
)


def _suggestion_restates_message(comment: ReviewComment) -> bool:
    """True when the suggestion adds nothing the message did not already say.

    Bag-of-words overlap measured against the *suggestion*: what fraction of
    the proposed change is already stated in the problem description. The 0.9
    threshold is deliberately high — 0.85 swallows legitimate "here is what the
    code does, here is what to change it to" pairs.
    """
    message = comment.message.lower().strip()
    suggestion = comment.suggestion.lower().strip()
    if not message or not suggestion:
        return False
    suggestion_words = set(suggestion.split())
    if not suggestion_words:
        return False
    overlap = len(set(message.split()) & suggestion_words) / len(suggestion_words)
    return overlap >= 0.9


def heuristic_verdict(
    comment: ReviewComment, change: FileChange
) -> QualifyVerdict | None:
    """Return a verdict when the heuristics are confident, else ``None``.

    Order matters: the cheapest and most certain discards run first, and
    anything that merely looks uncertain is routed to validation rather than
    resolved here.
    """
    if comment.severity is Severity.INFO:
        # Info comments are advisory; they never justify an agentic call.
        return QualifyVerdict.PASS

    if not any(ln.line_number in change.added_lines for ln in comment.line_numbers):
        return QualifyVerdict.DISCARD

    combined = f"{comment.message} {comment.suggestion}"

    # A fix narration is only a false positive when nothing follows it. A
    # contrastive clause after the match ("the fix correctly closes the
    # connection, *but* the handler still returns success") can hide a real
    # finding behind an acknowledgement, so route those to validation.
    for pattern in _BUGFIX_AS_ERROR_PATTERNS:
        match = pattern.search(combined)
        if not match:
            continue
        if _CONTRASTIVE_PATTERN.search(combined[match.end() :]):
            return QualifyVerdict.VALIDATE
        return QualifyVerdict.DISCARD

    if _suggestion_restates_message(comment):
        return QualifyVerdict.DISCARD

    if any(p.search(combined) for p in _UNCERTAINTY_PATTERNS):
        return QualifyVerdict.VALIDATE

    if any(p.search(combined) for p in _CROSS_FILE_PATTERNS):
        return QualifyVerdict.VALIDATE

    return None


class Qualifier:
    def __init__(self, client: DeepSeekClient, library: PromptLibrary):
        self._client = client
        self._library = library

    def qualify(
        self, comment: ReviewComment, change: FileChange, diff_snippet: str
    ) -> QualifyVerdict:
        decided = heuristic_verdict(comment, change)
        if decided is not None:
            log.debug("qualify(heuristic)=%s for %s", decided.value, change.filepath)
            return decided

        prompt = render(
            self._library.task("qualify_issue"),
            role_prompt=self._library.role("validator"),
            filepath=change.filepath,
            severity=comment.severity.value,
            category=comment.category.value,
            message=comment.message,
            suggestion=comment.suggestion or "(none provided)",
            criteria=comment.criteria,
            rule=comment.rule,
            diff_snippet=diff_snippet,
            scope_section=scope_section(change, comment),
        )
        try:
            text = self._client.complete_text(prompt, label=f"qualify:{change.filepath}")
        except Exception as exc:  # noqa: BLE001
            log.warning("qualify call failed, defaulting to validate: %s", exc)
            return QualifyVerdict.VALIDATE

        return _parse_keyword(text)


def _parse_keyword(text: str) -> QualifyVerdict:
    for line in reversed([ln.strip().strip("`").lower() for ln in text.splitlines()]):
        if not line:
            continue
        for verdict in QualifyVerdict:
            if line == verdict.value:
                return verdict
        break
    log.warning("unparseable qualify response, defaulting to validate")
    return QualifyVerdict.VALIDATE
