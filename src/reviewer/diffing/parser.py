"""Unified-diff parsing and gutter rendering.

The gutter is the ground truth for the scope check. Every rendered line is
prefixed with its old and new line numbers, so a citation the model emits can
be resolved against real numbers instead of trusted.

    ``{old:>6} | {new:>6} {marker}{content}``

Hunk headers are rendered without numbers in the gutter columns, which is what
makes "cited the ``@@`` line" detectable rather than silently accepted.
"""

from __future__ import annotations

import fnmatch
import logging
import re

from ..constants import REVIEW_IGNORE
from ..models import FileChange, Hunk

log = logging.getLogger(__name__)

_DIFF_GIT = re.compile(r"^diff --git a/(?P<old>.+?) b/(?P<new>.+?)$")
_HUNK = re.compile(
    r"^@@ -(?P<old_start>\d+)(?:,(?P<old_count>\d+))? "
    r"\+(?P<new_start>\d+)(?:,(?P<new_count>\d+))? @@(?P<tail>.*)$"
)


def should_ignore(filepath: str) -> bool:
    for pattern in REVIEW_IGNORE:
        if pattern.endswith("/"):
            if filepath.startswith(pattern) or f"/{pattern}" in f"/{filepath}":
                return True
        elif "*" in pattern:
            if fnmatch.fnmatch(filepath, pattern):
                return True
        elif filepath == pattern or filepath.endswith("/" + pattern):
            return True
    return False


def parse_unified_diff(text: str) -> list[FileChange]:
    """Parse ``git diff`` / GitHub patch text into per-file changes."""
    files: list[FileChange] = []
    current: FileChange | None = None
    hunk: Hunk | None = None
    old_no = new_no = 0
    buffer: list[str] = []

    def flush() -> None:
        nonlocal current, hunk
        if current is not None:
            current.raw_patch = "\n".join(buffer)
            files.append(current)
        current, hunk = None, None

    for line in text.splitlines():
        m = _DIFF_GIT.match(line)
        if m:
            flush()
            buffer = [line]
            current = FileChange(
                filepath=m.group("new"),
                old_path=m.group("old"),
                status="modified",
            )
            continue

        if current is None:
            continue
        buffer.append(line)

        if line.startswith("new file mode"):
            current.status = "added"
            continue
        if line.startswith("deleted file mode"):
            current.status = "deleted"
            continue
        if line.startswith("rename from") or line.startswith("rename to"):
            current.status = "renamed"
            continue
        if line.startswith(("index ", "--- ", "+++ ", "similarity index", "Binary files")):
            continue

        hm = _HUNK.match(line)
        if hm:
            old_no = int(hm.group("old_start"))
            new_no = int(hm.group("new_start"))
            hunk = Hunk(header=line, old_start=old_no, new_start=new_no)
            current.hunks.append(hunk)
            continue

        if hunk is None:
            continue

        if line.startswith("+"):
            hunk.lines.append(("+", None, new_no, line[1:]))
            new_no += 1
        elif line.startswith("-"):
            hunk.lines.append(("-", old_no, None, line[1:]))
            old_no += 1
        elif line.startswith("\\"):  # "\ No newline at end of file"
            hunk.lines.append((" ", None, None, line))
        else:
            content = line[1:] if line.startswith(" ") else line
            hunk.lines.append((" ", old_no, new_no, content))
            old_no += 1
            new_no += 1

    flush()
    kept = [f for f in files if not should_ignore(f.filepath)]

    # A combined diff names each file once. Repeats mean the input was a
    # per-commit patch series, whose intermediate line numbers do not match the
    # final state and would defeat the scope check.
    seen: set[str] = set()
    duplicates = {f.filepath for f in kept if f.filepath in seen or seen.add(f.filepath)}
    if duplicates:
        log.warning(
            "diff names %d file(s) more than once (%s); this looks like a "
            "per-commit patch series rather than a combined diff",
            len(duplicates),
            ", ".join(sorted(duplicates)[:3]),
        )

    return kept


def render_hunk(hunk: Hunk) -> str:
    out = [f"{'':>6} | {'':>6} {hunk.header}"]
    for marker, old, new, content in hunk.lines:
        old_col = f"{old:>6}" if old is not None else " " * 6
        new_col = f"{new:>6}" if new is not None else " " * 6
        out.append(f"{old_col} | {new_col} {marker}{content}")
    return "\n".join(out)


def render_diff(change: FileChange, hunks: list[Hunk] | None = None) -> str:
    """Render a file change with the line-number gutter."""
    selected = change.hunks if hunks is None else hunks
    if not selected:
        return f"(no textual diff for {change.filepath}; status={change.status})"
    return "\n".join(render_hunk(h) for h in selected)


def snippet_for_lines(change: FileChange, lines: set[int]) -> str:
    """Render only the hunks that contain any of ``lines``.

    Hunk granularity keeps the excerpt self-contained: a validator reading it
    sees the whole change region, not a window cut through the middle of one.
    """
    selected = [
        hunk
        for hunk in change.hunks
        if any(
            (new in lines and marker == "+") or (old in lines and marker == "-") or (new in lines)
            for marker, old, new, _content in hunk.lines
        )
    ]
    return render_diff(change, selected or change.hunks[:1])
