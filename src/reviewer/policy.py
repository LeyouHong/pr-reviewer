"""Policy routing: file path to the rule files that apply to it."""

from __future__ import annotations

import fnmatch
import json
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class MatchedPolicies:
    names: list[str]
    rule_paths: list[str]


@dataclass
class SemgrepSuppress:
    """One suppression entry. Path prefixes AND rule patterns must both match.

    ``reason`` is a required-in-practice human note explaining the trust
    boundary the suppression assumes. Unexplained suppressions rot: the next
    contributor cannot tell whether the rule is still safely ignored, so they
    either preserve it out of caution or delete it and reintroduce the noise.
    """

    path_prefixes: list[str] = field(default_factory=list)
    rule_patterns: list[str] = field(default_factory=list)
    reason: str = ""


@dataclass
class SemgrepOptions:
    """What semgrep configs to run and which findings to suppress.

    ``rules`` accepts the same forms semgrep itself does: registry shortcuts
    (``p/security-audit``) or absolute paths. ``exclude`` is passed straight
    through as ``--exclude`` arguments.
    """

    rules: list[str] = field(default_factory=list)
    exclude: list[str] = field(default_factory=list)
    suppress: list[SemgrepSuppress] = field(default_factory=list)

    @property
    def enabled(self) -> bool:
        return bool(self.rules)


class PolicyRouter:
    def __init__(self, resources: Path, config_name: str = "default"):
        raw = json.loads(
            (resources / "policies" / "default.json").read_text(encoding="utf-8")
        )
        self._config = raw.get(config_name, {})
        self._policies: dict = self._config.get("policies", {})
        self._exclude: list[str] = self._config.get("exclude_patterns", [])
        self._semgrep = self._load_semgrep(self._config.get("semgrep", {}))

    @staticmethod
    def _load_semgrep(block: dict) -> SemgrepOptions:
        return SemgrepOptions(
            rules=list(block.get("rules", [])),
            exclude=list(block.get("exclude", [])),
            suppress=[
                SemgrepSuppress(
                    path_prefixes=list(entry.get("path_prefixes", [])),
                    rule_patterns=list(entry.get("rule_patterns", [])),
                    reason=str(entry.get("reason", "")),
                )
                for entry in block.get("suppress", [])
            ],
        )

    def excluded(self, filepath: str) -> bool:
        return any(fragment in f"/{filepath}" for fragment in self._exclude)

    def match(self, filepath: str) -> MatchedPolicies:
        names: list[str] = []
        paths: list[str] = []
        for name, policy in self._policies.items():
            if not self._matches(filepath, policy.get("rules", [])):
                continue
            names.append(name)
            prompt = policy.get("apply_prompt")
            if prompt and prompt != "default":
                paths.append(prompt)
        return MatchedPolicies(names=names, rule_paths=paths)

    def semgrep_options(self) -> SemgrepOptions:
        return self._semgrep

    @staticmethod
    def _matches(filepath: str, rules: list[dict]) -> bool:
        for rule in rules:
            if rule.get("ruletype") != "pattern-match":
                continue
            pattern = rule.get("pattern", "")
            if pattern == "*":
                return True
            base = filepath.rsplit("/", 1)[-1]
            if fnmatch.fnmatch(filepath, pattern) or fnmatch.fnmatch(base, pattern):
                return True
        return False


def is_suppressed(
    filepath: str, rule_id: str, suppress: list[SemgrepSuppress]
) -> str | None:
    """Return the suppression reason if ``(filepath, rule_id)`` matches an entry.

    Both dimensions must match: a path prefix hit on its own does not suppress
    every rule under that prefix, and vice versa. This asymmetry is deliberate
    — a targeted suppression is auditable, a blanket one hides everything.
    Empty ``path_prefixes`` or ``rule_patterns`` matches any value on that
    dimension, so a suppression by rule id alone is expressible.
    """
    for entry in suppress:
        prefix_ok = not entry.path_prefixes or any(
            filepath.startswith(prefix) for prefix in entry.path_prefixes
        )
        if not prefix_ok:
            continue
        rule_ok = not entry.rule_patterns or any(
            fnmatch.fnmatch(rule_id, pattern) for pattern in entry.rule_patterns
        )
        if rule_ok:
            return entry.reason or "(suppressed)"
    return None
