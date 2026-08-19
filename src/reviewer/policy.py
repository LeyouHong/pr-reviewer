"""Policy routing: file path to the rule files that apply to it."""

from __future__ import annotations

import fnmatch
import json
from dataclasses import dataclass
from pathlib import Path


@dataclass
class MatchedPolicies:
    names: list[str]
    rule_paths: list[str]


class PolicyRouter:
    def __init__(self, resources: Path, config_name: str = "default"):
        raw = json.loads(
            (resources / "policies" / "default.json").read_text(encoding="utf-8")
        )
        self._config = raw.get(config_name, {})
        self._policies: dict = self._config.get("policies", {})
        self._exclude: list[str] = self._config.get("exclude_patterns", [])

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
