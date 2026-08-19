"""Read-only filesystem surface exposed to the agentic validator.

Four tools, all rooted at the repository under review. Paths are resolved and
checked against the root, so a model that asks for ``../../etc/passwd`` gets an
error string rather than a file. Tool errors are returned as data — an agent
that mistypes a path should correct itself, not crash the run.
"""

from __future__ import annotations

import fnmatch
import re
from pathlib import Path
from typing import Any

from ..constants import TOOL_OUTPUT_CHAR_LIMIT

TOOL_SPECS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "list_directory",
            "description": (
                "List files and directories at a repository-relative path. "
                "Use targeted paths; do not list the repository root recursively."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Repository-relative directory path, e.g. 'src/api'.",
                    }
                },
                "required": ["path"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": (
                "Read a file's contents with line numbers. Use offset and "
                "max_lines to target a range instead of reading whole files."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Repository-relative file path, with no leading slash.",
                    },
                    "offset": {
                        "type": "integer",
                        "description": "1-indexed first line to read. Defaults to 1.",
                    },
                    "max_lines": {
                        "type": "integer",
                        "description": "How many lines to read. Defaults to 400.",
                    },
                },
                "required": ["path"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_files",
            "description": "Search file contents for a regular expression.",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "Python regex."},
                    "glob": {
                        "type": "string",
                        "description": "Restrict to files matching this glob, e.g. '**/*.java'.",
                    },
                    "context_lines": {
                        "type": "integer",
                        "description": "Lines of surrounding context. Defaults to 2.",
                    },
                },
                "required": ["pattern"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "glob_files",
            "description": "Find files matching a glob pattern, e.g. 'src/**/*.go'.",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "Glob pattern."}
                },
                "required": ["pattern"],
                "additionalProperties": False,
            },
        },
    },
]

_SKIP_DIRS = {".git", "node_modules", "venv", ".venv", "__pycache__", "target", "dist", "build"}
_MAX_MATCHES = 80


class FileSystemTools:
    def __init__(self, root: Path):
        self.root = root.resolve()

    # -- path safety ------------------------------------------------------

    def _resolve(self, raw: str) -> Path:
        cleaned = (raw or "").strip().lstrip("/")
        target = (self.root / cleaned).resolve()
        if target != self.root and self.root not in target.parents:
            raise ValueError(f"path escapes the repository root: {raw!r}")
        return target

    # -- dispatch ---------------------------------------------------------

    def dispatch(self, name: str, args: dict[str, Any]) -> str:
        handler = {
            "list_directory": self.list_directory,
            "read_file": self.read_file,
            "search_files": self.search_files,
            "glob_files": self.glob_files,
        }.get(name)
        if handler is None:
            return f"ERROR: unknown tool {name!r}"
        return handler(**args)[:TOOL_OUTPUT_CHAR_LIMIT]

    # -- tools ------------------------------------------------------------

    def list_directory(self, path: str = ".") -> str:
        target = self._resolve(path)
        if not target.is_dir():
            return f"ERROR: not a directory: {path}"
        rows = []
        for entry in sorted(target.iterdir(), key=lambda p: (p.is_file(), p.name)):
            if entry.name in _SKIP_DIRS:
                continue
            rows.append(f"{'dir ' if entry.is_dir() else 'file'}  {entry.name}")
        return "\n".join(rows) or "(empty)"

    def read_file(self, path: str, offset: int = 1, max_lines: int = 400) -> str:
        target = self._resolve(path)
        if not target.is_file():
            return f"ERROR: not a file: {path}"
        try:
            lines = target.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError as exc:
            return f"ERROR: {exc}"
        start = max(offset, 1)
        window = lines[start - 1 : start - 1 + max(max_lines, 1)]
        if not window:
            return f"(no lines at offset {start}; file has {len(lines)} lines)"
        body = "\n".join(f"{start + i:>6}  {text}" for i, text in enumerate(window))
        tail = ""
        if start - 1 + len(window) < len(lines):
            tail = f"\n... ({len(lines) - (start - 1 + len(window))} more lines)"
        return body + tail

    def glob_files(self, pattern: str) -> str:
        matches = []
        for candidate in self.root.rglob("*"):
            if not candidate.is_file():
                continue
            if any(part in _SKIP_DIRS for part in candidate.parts):
                continue
            rel = candidate.relative_to(self.root).as_posix()
            if fnmatch.fnmatch(rel, pattern) or fnmatch.fnmatch(rel, f"**/{pattern}"):
                matches.append(rel)
            if len(matches) >= _MAX_MATCHES:
                break
        return "\n".join(matches) or f"(no files match {pattern!r})"

    def search_files(
        self, pattern: str, glob: str = "**/*", context_lines: int = 2
    ) -> str:
        try:
            regex = re.compile(pattern)
        except re.error as exc:
            return f"ERROR: invalid regex: {exc}"

        out: list[str] = []
        hits = 0
        for candidate in self.root.rglob("*"):
            if hits >= _MAX_MATCHES:
                break
            if not candidate.is_file():
                continue
            if any(part in _SKIP_DIRS for part in candidate.parts):
                continue
            rel = candidate.relative_to(self.root).as_posix()
            if not (fnmatch.fnmatch(rel, glob) or fnmatch.fnmatch(rel, f"**/{glob}")):
                continue
            try:
                lines = candidate.read_text(encoding="utf-8", errors="replace").splitlines()
            except OSError:
                continue
            for i, text in enumerate(lines):
                if not regex.search(text):
                    continue
                hits += 1
                lo = max(i - context_lines, 0)
                hi = min(i + context_lines + 1, len(lines))
                out.append(f"--- {rel}:{i + 1}")
                out.extend(f"{n + 1:>6}  {lines[n]}" for n in range(lo, hi))
                if hits >= _MAX_MATCHES:
                    break
        return "\n".join(out) or f"(no matches for {pattern!r})"
