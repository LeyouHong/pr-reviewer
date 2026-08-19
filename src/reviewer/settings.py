"""Scan configuration.

The scan loop is what turns a one-shot ``review-pr`` into a cron. Its
inputs — which repos, which target branches, how far back to look, when to
soften the caps — live in a JSON file rather than the environment so two
operators on different machines share one authoritative source.

The shape mirrors the reference implementation so a user familiar with that
one can migrate settings without translation. Windows are defined in a named
timezone; a laptop that suspends across midnight still sees the same window
boundary as the machine that produced the file.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, time
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo


@dataclass
class WindowConfig:
    """Overrides that apply when the local clock is inside this window.

    ``timerange`` is the look-back applied when the scanner asks "has this PR
    been touched recently?" — a wider window during off-hours picks up PRs
    the daytime pass may have skipped. ``max_files`` caps how much of a large
    PR the reviewer will attempt. Both default to ``None``, meaning "no
    override; use the caller-level default".
    """

    max_files: Optional[int] = None
    timerange: Optional[int] = None
    start: str = "00:00"
    end: str = "00:00"

    def contains(self, now: datetime) -> bool:
        """True when ``now``'s wall-clock time falls in ``[start, end)``.

        A window that wraps midnight (``start > end``) is treated as
        two half-open intervals so ``21:00`` — ``06:00`` behaves correctly.
        """
        current = now.time()
        start = _parse_hhmm(self.start)
        end = _parse_hhmm(self.end)
        if start <= end:
            return start <= current < end
        return current >= start or current < end


@dataclass
class OperationConfig:
    timezone: str = "UTC"
    active: WindowConfig = field(default_factory=WindowConfig)
    inactive: WindowConfig = field(default_factory=WindowConfig)

    def window_for(self, now: datetime | None = None) -> WindowConfig | None:
        """Return the window covering ``now``, or ``None`` if neither applies.

        ``None`` is a legitimate answer: a partial settings file with only an
        ``active`` block leaves off-hours uncovered on purpose, and the caller
        should fall back to its defaults rather than silently pretend one
        window is always on.
        """
        clock = now or datetime.now(tz=ZoneInfo(self.timezone))
        for window in (self.active, self.inactive):
            if _window_defined(window) and window.contains(clock):
                return window
        return None


@dataclass
class RepoConfig:
    url: str
    target_branches: list[str] = field(default_factory=lambda: ["main"])
    timerange: int = 14 * 24 * 3600  # 14 days, matching the reference default.


@dataclass
class ScanSettings:
    repositories: list[RepoConfig] = field(default_factory=list)
    operation: OperationConfig = field(default_factory=OperationConfig)


def load_settings(path: Path) -> ScanSettings:
    raw = json.loads(path.read_text(encoding="utf-8"))
    repos_block = raw.get("repositories") or {}
    repositories = [
        RepoConfig(
            url=url,
            target_branches=list(cfg.get("target-branches") or ["main"]),
            timerange=int(cfg.get("timerange") or (14 * 24 * 3600)),
        )
        for url, cfg in repos_block.items()
    ]
    op_block = raw.get("operation") or {}
    hours = op_block.get("hours") or {}
    operation = OperationConfig(
        timezone=str(op_block.get("timezone") or "UTC"),
        active=_load_window(hours.get("active")),
        inactive=_load_window(hours.get("inactive")),
    )
    return ScanSettings(repositories=repositories, operation=operation)


def _load_window(block: dict | None) -> WindowConfig:
    block = block or {}
    return WindowConfig(
        max_files=block.get("max_files"),
        timerange=block.get("timerange"),
        start=str(block.get("start") or "00:00"),
        end=str(block.get("end") or "00:00"),
    )


def _parse_hhmm(value: str) -> time:
    hour, minute = value.split(":", 1)
    return time(hour=int(hour), minute=int(minute))


def _window_defined(window: WindowConfig) -> bool:
    """A default-constructed window has ``start == end == "00:00"`` and no overrides.

    Distinguishing "not configured" from "configured to a 0-length window" lets
    the loader accept partial settings without pretending a missing block is
    always-on.
    """
    if window.start != "00:00" or window.end != "00:00":
        return True
    return window.max_files is not None or window.timerange is not None
