"""Prompt composition.

Templates use ``{placeholder}`` syntax, but ``str.format`` is deliberately not
used: these prompts embed JSON and code samples full of literal braces, and
``str.format`` would either crash on them or force every example to be written
with doubled braces. :func:`render` substitutes only the keys it was given and
leaves every other brace untouched.
"""

from __future__ import annotations

import logging
import re
from functools import lru_cache
from pathlib import Path

log = logging.getLogger(__name__)

_PLACEHOLDER = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")


def render(template: str, **values: object) -> str:
    """Substitute known placeholders; leave unknown braces verbatim."""

    def _sub(match: re.Match[str]) -> str:
        key = match.group(1)
        if key in values:
            return str(values[key])
        return match.group(0)

    return _PLACEHOLDER.sub(_sub, template)


class PromptLibrary:
    def __init__(self, resources: Path):
        self.resources = Path(resources)

    @lru_cache(maxsize=64)
    def _read(self, relative: str) -> str:
        path = self.resources / relative.lstrip("/")
        if not path.is_file():
            # A silent miss used to inject placeholder text into the prompt and
            # the model would then reason around it. Fail loudly instead.
            log.warning("missing prompt resource: %s", path)
            return f"[Missing prompt resource: {relative}]"
        return path.read_text(encoding="utf-8").strip()

    # -- roles ------------------------------------------------------------

    def role(self, name: str) -> str:
        return self._read(f"role/{name}.md")

    # -- tasks ------------------------------------------------------------

    def task(self, name: str) -> str:
        return self._read(f"tasks/{name}.md")

    # -- rules ------------------------------------------------------------

    def rules(self, relative: str) -> str:
        return self._read(relative)

    def concat_rules(self, paths: list[str]) -> str:
        blocks = [self.rules(p) for p in paths]
        return "\n\n".join(b for b in blocks if b)


def file_changes_block(
    filepath: str,
    rendered_diff: str,
    general_notes: str = "",
    chunk_index: int = 0,
    chunk_total: int = 1,
) -> str:
    """Build the fenced per-file context the reviewer prompt embeds."""
    parts: list[str] = []
    if chunk_total > 1:
        parts.append(
            f"> Note: `{filepath}` was split into {chunk_total} chunks for review. "
            f"This is chunk {chunk_index + 1}. Findings must cite lines present "
            f"in this chunk."
        )
    parts.append(f"````file-diff path=/{filepath}\n{rendered_diff}\n````")
    if general_notes.strip():
        parts.append(f"````file-general-notes path=/{filepath}\n{general_notes}\n````")
    return "\n\n".join(parts)
