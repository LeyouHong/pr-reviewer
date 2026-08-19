"""Scope enforcement.

A review grades what the change added. Two failure modes this guards against:

* **fabricated citations** — a line number that appears nowhere in the rendered
  gutter. Such a citation cannot be checked by a human either, so it is dropped.
* **out-of-scope findings** — every cited line resolves, but none of them is a
  line this change added. The claim may be true; it is not this PR's business.

Run this twice: once on each ensemble member's output, and again after
aggregation, because aggregation can introduce line citations that no
constituent review made.
"""

from __future__ import annotations

from ..models import FileChange, LineState, ReviewComment


def _gutter_lines(change: FileChange) -> tuple[set[int], set[int], set[int]]:
    """Return (added_new_lines, removed_old_lines, context_new_lines)."""
    added: set[int] = set()
    removed: set[int] = set()
    context: set[int] = set()
    for hunk in change.hunks:
        for marker, old, new, _content in hunk.lines:
            if marker == "+" and new is not None:
                added.add(new)
            elif marker == "-" and old is not None:
                removed.add(old)
            elif marker == " " and new is not None:
                context.add(new)
    return added, removed, context


def citation_resolves(change: FileChange, line: int, state: LineState) -> bool:
    added, removed, context = _gutter_lines(change)
    if state is LineState.DIFF_ADDED:
        return line in added
    if state is LineState.DIFF_REMOVED:
        return line in removed
    return line in context or line in added


def filter_in_scope(
    change: FileChange, comments: list[ReviewComment]
) -> tuple[list[ReviewComment], list[ReviewComment]]:
    """Split ``comments`` into (in_scope, out_of_scope)."""
    kept: list[ReviewComment] = []
    dropped: list[ReviewComment] = []
    added, _removed, _context = _gutter_lines(change)

    for comment in comments:
        resolved = [
            ln
            for ln in comment.line_numbers
            if citation_resolves(change, ln.line_number, ln.line_number_state)
        ]
        if not resolved:
            dropped.append(comment)
            continue
        touches_added = any(ln.line_number in added for ln in resolved)
        if not touches_added:
            dropped.append(comment)
            continue
        comment.line_numbers = resolved
        kept.append(comment)

    return kept, dropped


def scope_section(change: FileChange, comment: ReviewComment) -> str:
    """Pre-computed scope facts for the qualification prompt.

    The model is bad at deriving this from a raw diff, so it is stated as a
    finding rather than asked as a question.
    """
    added, _removed, _context = _gutter_lines(change)
    cited = [ln.line_number for ln in comment.line_numbers]
    inside = sorted(n for n in cited if n in added)
    outside = sorted(n for n in cited if n not in added)

    ranges = _compress(sorted(added))
    lines = [f"Lines added by this diff in `{change.filepath}`: {ranges or 'none'}"]
    lines.append(f"Lines cited by this issue: {cited or 'none'}")
    if inside:
        lines.append(f"Cited lines that this diff added: {inside}")
    else:
        lines.append(
            "None of the cited lines were added by this diff."
        )
    if outside:
        lines.append(f"Cited lines outside the added set: {outside}")
    return "\n".join(lines)


def _compress(numbers: list[int]) -> str:
    if not numbers:
        return ""
    spans: list[str] = []
    start = prev = numbers[0]
    for n in numbers[1:]:
        if n == prev + 1:
            prev = n
            continue
        spans.append(str(start) if start == prev else f"{start}-{prev}")
        start = prev = n
    spans.append(str(start) if start == prev else f"{start}-{prev}")
    return ", ".join(spans)
