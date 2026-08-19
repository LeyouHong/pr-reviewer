"""Split an oversized file diff into reviewable chunks.

With a 1M-token context this is a safety net, not a routine step: it only
engages for pathological files. Splits happen at ``@@`` boundaries so hunk
semantics survive; a single hunk that exceeds the budget on its own is split at
the line level with a synthetic header so the gutter stays meaningful.
"""

from __future__ import annotations

from ..constants import CHUNK_TOKEN_BUDGET
from ..models import FileChange, Hunk
from .parser import render_hunk
from .tokens import count_tokens


def _split_oversized(hunk: Hunk, budget: int) -> list[Hunk]:
    out: list[Hunk] = []
    current = Hunk(header=hunk.header, old_start=hunk.old_start, new_start=hunk.new_start)
    size = count_tokens(hunk.header)

    for entry in hunk.lines:
        cost = count_tokens(entry[3]) + 8  # gutter overhead
        if current.lines and size + cost > budget:
            out.append(current)
            first = current.lines[-1]
            current = Hunk(
                header=f"{hunk.header}  (continued)",
                old_start=first[1] or hunk.old_start,
                new_start=first[2] or hunk.new_start,
            )
            size = count_tokens(current.header)
        current.lines.append(entry)
        size += cost

    if current.lines:
        out.append(current)
    return out or [hunk]


def chunk_file_change(
    change: FileChange, budget: int = CHUNK_TOKEN_BUDGET
) -> list[list[Hunk]]:
    """Group a change's hunks into chunks that each fit the token budget."""
    chunks: list[list[Hunk]] = []
    current: list[Hunk] = []
    size = 0

    for hunk in change.hunks:
        cost = count_tokens(render_hunk(hunk))
        if cost > budget:
            if current:
                chunks.append(current)
                current, size = [], 0
            for piece in _split_oversized(hunk, budget):
                chunks.append([piece])
            continue
        if current and size + cost > budget:
            chunks.append(current)
            current, size = [], 0
        current.append(hunk)
        size += cost

    if current:
        chunks.append(current)
    return chunks or [[]]
