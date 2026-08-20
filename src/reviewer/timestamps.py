"""One place to compare the timestamps GitHub hands back.

Every ordering decision the scanner makes — is this trigger newer than the
report that would have answered it, has a report landed since the author last
pushed — rests on comparing two ISO-8601 strings. Comparing them *as strings*
works only while every producer agrees on a spelling, and they do not: the
REST API returns ``2026-08-19T11:32:42Z`` while anything formatted by
``datetime.isoformat`` returns ``2026-08-19T11:32:42+00:00``. Those two are the
same instant and compare unequal, with ``Z`` sorting after ``+`` — so the
newer moment can lose. Parsing first makes the comparison mean what it says.
"""

from __future__ import annotations

from datetime import datetime, timezone


def parse_iso(value: str | None) -> datetime | None:
    """Parse an ISO-8601 timestamp into an aware datetime, or ``None``.

    A naive timestamp is read as UTC: every producer here emits UTC, and
    guessing the local zone would silently shift comparisons by hours.
    """
    if not value:
        return None
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def is_after(candidate: str | None, reference: str | None) -> bool:
    """True when ``candidate`` is strictly later than ``reference``.

    An unparseable or missing ``reference`` means "no bound", so anything
    counts as after it — a trigger with no report to have answered it is still
    live. An unparseable ``candidate`` is not after anything, because acting on
    a timestamp we cannot read is the riskier direction.
    """
    left = parse_iso(candidate)
    if left is None:
        return False
    right = parse_iso(reference)
    return right is None or left > right


def newest(values: list[str]) -> str:
    """Return the latest parseable timestamp, or ``""``.

    Returns the original string rather than a datetime so callers can hand it
    straight back to an API that expects the wire format.
    """
    best_text, best_dt = "", None
    for value in values:
        parsed = parse_iso(value)
        if parsed is not None and (best_dt is None or parsed > best_dt):
            best_text, best_dt = value, parsed
    return best_text


__all__ = ["is_after", "newest", "parse_iso"]
