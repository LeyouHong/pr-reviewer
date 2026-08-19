"""Stage 2: agentic per-issue validation."""

from __future__ import annotations

import logging
import re

from .. import constants
from ..models import FileChange, ReviewComment, ValidateVerdict
from ..prompt import PromptLibrary, render
from ..provider import DeepSeekClient
from ..tools.fs_tools import TOOL_SPECS, FileSystemTools

log = logging.getLogger(__name__)

_VERDICT = re.compile(
    r"Verdict:\s*(TRUE_POSITIVE_SEVERITY_INFO|TRUE_POSITIVE|FALSE_POSITIVE_OOS"
    r"|FALSE_POSITIVE|INDETERMINATE)",
    re.IGNORECASE,
)


class Validator:
    def __init__(
        self,
        client: DeepSeekClient,
        library: PromptLibrary,
        tools: FileSystemTools,
    ):
        self._client = client
        self._library = library
        self._tools = tools

    def validate(
        self,
        comment: ReviewComment,
        change: FileChange,
        diff_snippet: str,
        policy_rules: str,
    ) -> ValidateVerdict:
        prompt = render(
            self._library.task("validate_issue"),
            role_prompt=self._library.role("validator"),
            filepath=change.filepath,
            severity=comment.severity.value,
            category=comment.category.value,
            message=comment.message,
            suggestion=comment.suggestion or "(none provided)",
            criteria=comment.criteria,
            rule=comment.rule,
            diff_snippet=diff_snippet,
            policy_rules=policy_rules or "(no additional policy rules apply)",
        )

        try:
            result = self._client.run_agent(
                prompt,
                tool_specs=TOOL_SPECS,
                dispatch=self._tools.dispatch,
                max_turns=constants.MAX_TURNS_VALIDATE,
                label=f"validate:{change.filepath}",
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("validation failed, keeping issue as indeterminate: %s", exc)
            return ValidateVerdict.INDETERMINATE

        if result.turn_limit_reached:
            log.warning("validator hit the turn limit for %s", change.filepath)
            return ValidateVerdict.INDETERMINATE

        verdict = _parse_verdict(result.final_output)

        # A verdict reached without touching the codebase is an opinion, not a
        # finding. The prompt says so; this enforces it.
        if verdict is not ValidateVerdict.INDETERMINATE and not result.used_tools:
            log.info(
                "validator reached %s without tool calls; downgrading to INDETERMINATE",
                verdict.value,
            )
            return ValidateVerdict.INDETERMINATE

        return verdict


def _parse_verdict(text: str) -> ValidateVerdict:
    matches = _VERDICT.findall(text or "")
    if not matches:
        log.warning("no parseable verdict; treating as indeterminate")
        return ValidateVerdict.INDETERMINATE
    return ValidateVerdict(matches[-1].upper())
