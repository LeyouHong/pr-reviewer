"""Semgrep static-analysis pass, folded into the review flow.

Semgrep is a second reviewer with a different failure mode from the LLM: the
LLM misses patterns that are obvious to a rule engine and hallucinates ones
that aren't there. Merging the two sources of findings means each covers the
other's blind spot — but only if they are treated identically downstream.

The coercion keeps that promise: a semgrep hit lands as a
:class:`ReviewComment` with the same schema the LLM emits, so the scope
filter, qualification gate, and deep validator process it without a special
branch. A false-positive rule then earns the same skepticism as a false-
positive LLM claim.
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from ..models import (
    Category,
    Complexity,
    FileChange,
    LineNumber,
    LineState,
    ReviewComment,
    Severity,
)
from ..policy import SemgrepOptions, SemgrepSuppress, is_suppressed

log = logging.getLogger(__name__)

# Semgrep exits 0 on clean and 1 on findings; anything else is an execution
# failure the caller cannot recover from silently.
_SEMGREP_OK_EXIT_CODES = (0, 1)

# Map semgrep severities to our own. UPPERCASE is what semgrep emits.
_SEVERITY_MAP: dict[str, Severity] = {
    "ERROR": Severity.ERROR,
    "WARNING": Severity.WARNING,
    "INFO": Severity.INFO,
}


def _staging_dir_for(changes: list[FileChange], repo_root: Path) -> Path | None:
    """Copy the current content of each changed file into a scratch tree.

    Semgrep needs a real filesystem to walk; giving it the whole repo checkout
    would run rules over files the PR did not touch and inflate cost linearly
    with repo size. The scratch tree preserves relative paths (semgrep uses
    them in ``check_id`` output), so a finding's ``path`` still maps 1:1 back
    to the ``FileChange``.
    """
    scratch = Path(tempfile.mkdtemp(prefix="pr-reviewer-semgrep-"))
    copied = 0
    for change in changes:
        if change.status == "deleted":
            continue
        source = repo_root / change.filepath
        if not source.is_file():
            log.info("semgrep: missing %s in checkout; skipping", change.filepath)
            continue
        dest = scratch / change.filepath
        dest.parent.mkdir(parents=True, exist_ok=True)
        try:
            shutil.copy2(source, dest)
        except OSError as exc:
            log.warning("semgrep: failed to stage %s: %s", change.filepath, exc)
            continue
        copied += 1

    if copied == 0:
        shutil.rmtree(scratch, ignore_errors=True)
        return None
    return scratch


def _run_semgrep(scratch: Path, options: SemgrepOptions) -> list[dict[str, Any]]:
    """Invoke ``semgrep scan`` and return the parsed ``results`` list.

    A missing semgrep binary is common — the tool is opt-in — so the failure
    is logged and translated to no findings rather than raised. That keeps
    ``--semgrep`` safe to enable in environments that may or may not have the
    binary installed.
    """
    if not shutil.which("semgrep"):
        log.warning("semgrep binary not on PATH; skipping the static-analysis pass")
        return []

    cmd = ["semgrep", "scan"]
    for rule in options.rules:
        cmd.append(f"--config={rule}")
    for pattern in options.exclude:
        cmd.append(f"--exclude={pattern}")
    cmd += [
        "--json",
        "--no-git-ignore",
        "--disable-version-check",
        "--metrics=off",
        ".",
    ]

    log.info("semgrep: running %s in %s", " ".join(cmd), scratch)
    result = subprocess.run(
        cmd, capture_output=True, text=True, cwd=str(scratch), check=False
    )
    if result.returncode not in _SEMGREP_OK_EXIT_CODES:
        log.error(
            "semgrep exited with %d: %s",
            result.returncode,
            result.stderr[:400],
        )
        return []

    try:
        payload = json.loads(result.stdout or "{}")
    except json.JSONDecodeError as exc:
        log.error("semgrep output was not valid JSON: %s", exc)
        return []
    return list(payload.get("results", []))


def _coerce_to_comment(
    finding: dict[str, Any], change: FileChange
) -> ReviewComment | None:
    """Translate one semgrep finding into a :class:`ReviewComment`.

    The line-state tag drives the scope check: if the reported line is not an
    added line, the finding is out of scope for this PR (semgrep sees the
    whole file; the reviewer only speaks to the diff). ``FILE_CONTEXT`` marks
    those; ``filter_in_scope`` drops them.
    """
    start = finding.get("start") or {}
    line = start.get("line")
    if not isinstance(line, int) or line < 1:
        return None

    added = line in change.added_lines
    state = LineState.DIFF_ADDED if added else LineState.FILE_CONTEXT
    rule_id = finding.get("check_id", "semgrep")
    extra = finding.get("extra") or {}
    severity = _SEVERITY_MAP.get(str(extra.get("severity", "")).upper(), Severity.WARNING)
    message = str(extra.get("message", "")).strip() or rule_id
    metadata = extra.get("metadata") or {}
    category_raw = str(metadata.get("category", "")).lower()
    category = _category_from(category_raw)

    return ReviewComment(
        line_numbers=[LineNumber(line_number=line, line_number_state=state)],
        severity=severity,
        category=category,
        message=f"[semgrep:{rule_id}] {message}",
        criteria=rule_id,
        suggestion=str(extra.get("fix") or "").strip(),
        rule=rule_id,
        implementation_complexity=Complexity.LOW,
        context_needed=not added,
    )


def _category_from(raw: str) -> Category:
    """Semgrep's category taxonomy is broader than ours; project onto our enum."""
    if "security" in raw:
        return Category.SECURITY
    if "performance" in raw:
        return Category.PERFORMANCE
    if "maintainability" in raw or "best-practice" in raw:
        return Category.MAINTAINABILITY
    if "correctness" in raw or "logic" in raw:
        return Category.LOGIC
    if "testing" in raw or "test" in raw:
        return Category.TESTING
    return Category.LOGIC


def collect_findings(
    changes: list[FileChange],
    repo_root: Path,
    options: SemgrepOptions,
) -> dict[str, list[ReviewComment]]:
    """Run semgrep and return findings grouped by filepath.

    Files with no findings do not appear in the result — the caller merges by
    filepath, so absence and empty-list are equivalent for downstream code.
    """
    if not options.enabled:
        return {}

    changed_by_path = {c.filepath: c for c in changes}
    scratch = _staging_dir_for(changes, repo_root)
    if scratch is None:
        return {}
    try:
        raw = _run_semgrep(scratch, options)
    finally:
        shutil.rmtree(scratch, ignore_errors=True)

    grouped: dict[str, list[ReviewComment]] = {}
    for finding in raw:
        path = finding.get("path", "")
        change = changed_by_path.get(path)
        if change is None:
            # The staging tree only contains changed files, but semgrep can
            # report on a sub-path (e.g. a symlink target) that no FileChange
            # covers. Drop those — we can't scope-check them.
            continue

        rule_id = str(finding.get("check_id", ""))
        reason = is_suppressed(path, rule_id, options.suppress)
        if reason is not None:
            log.info(
                "semgrep: suppressing %s in %s (%s)", rule_id, path, reason
            )
            continue

        comment = _coerce_to_comment(finding, change)
        if comment is None:
            continue
        grouped.setdefault(path, []).append(comment)
    return grouped


def coerce_finding(finding: dict[str, Any], change: FileChange) -> ReviewComment | None:
    """Public wrapper for :func:`_coerce_to_comment`, for tests and injection."""
    return _coerce_to_comment(finding, change)


__all__ = [
    "SemgrepOptions",
    "SemgrepSuppress",
    "collect_findings",
    "coerce_finding",
]
