"""Synthetic ``research_codebase`` tool.

The base filesystem tools are load-bearing but low-level: answering "is method
X called anywhere else in the tree?" as an outer-loop question costs the
reviewer three or four rounds of ``search_files`` → ``read_file`` →
``search_files`` → prose synthesis, and the model has to hold the accumulating
context itself. That accumulation is expensive in tokens and, worse, tends to
crowd out the diff the reviewer is supposed to be judging.

Wrapping that pattern in a tool lets the outer reviewer offload the whole
exploration to a nested agent with its own context window. The reviewer sees
one ``tool_output`` per research call; the inner agent's turns do not count
against the reviewer's ``max_turns``. In exchange, we pay for a nested LLM
loop — so the tool description is careful about when it earns its cost.

The nested agent is deliberately given *only* the base fs tools. If it could
call ``research_codebase`` itself, a single "explore the codebase" prompt
would recurse until it hits some limit or another, and the failure mode would
be silent burn rather than a bad answer.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Iterable

from ..constants import TOOL_OUTPUT_CHAR_LIMIT
from ..prompt import PromptLibrary, render
from ..provider import ProviderClient
from .fs_tools import TOOL_SPECS as _BASE_TOOL_SPECS, FileSystemTools

log = logging.getLogger(__name__)

# Inner researcher gets a small turn budget on purpose. Answering "where is
# method X called?" needs 3–5 tool calls; anything past that is either the
# question being wrong or the researcher going in circles, and burning 15
# turns to arrive at "unclear" is worse than saying so after 8.
DEFAULT_RESEARCH_TURNS = 8

# Each research call is a whole nested agent loop, and the outer reviewer gets
# twenty turns — so an unbudgeted reviewer can spend twenty nested loops on one
# file without ever being wrong enough to stop. Five is generous for the
# questions this is meant to answer; past that the reviewer is browsing, and
# the refusal tells it to conclude with what it has rather than failing the
# review.
DEFAULT_RESEARCH_CALLS = 5


RESEARCH_TOOL_SPEC: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "research_codebase",
        "description": (
            "Answer one targeted factual question about the repository by "
            "searching and reading files in a nested exploration loop. Prefer "
            "this over sequential search_files + read_file when the answer "
            "requires synthesising two or more sources — e.g. 'is method X "
            "called anywhere else?', 'what value does helper Y return in the "
            "error path?', 'where is field Z initialised?'. Do NOT use it for "
            "questions you can settle with one read_file. Vague prompts "
            "('explore the codebase', 'summarise this module') produce vague "
            "answers; the tool is cheapest when the question names a specific "
            "symbol, invariant, or behaviour."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "question": {
                    "type": "string",
                    "description": (
                        "One factual question. Name a specific symbol, "
                        "expression, or behaviour. Ten to thirty words is "
                        "typical; multi-question prompts are answered poorly."
                    ),
                },
                "focus_paths": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Optional repository-relative paths to prioritise "
                        "when searching. Leave empty to search the whole "
                        "repository."
                    ),
                },
            },
            "required": ["question"],
            "additionalProperties": False,
        },
    },
}


def extended_tool_specs(base: Iterable[dict[str, Any]] = _BASE_TOOL_SPECS) -> list[dict[str, Any]]:
    """Return the outer reviewer's tool set: base fs tools + research."""
    return [*base, RESEARCH_TOOL_SPEC]


class Researcher:
    """Runs a nested read-only agent loop to answer one research question.

    The client, library, and fs tools are injected so the researcher can be
    swapped or stubbed without touching the review pipeline. ``max_turns``
    caps the inner exploration; the returned string is either the model's
    final prose answer or a diagnostic message the outer reviewer can quote.
    """

    def __init__(
        self,
        client: ProviderClient,
        library: PromptLibrary,
        fs_tools: FileSystemTools,
        *,
        max_turns: int = DEFAULT_RESEARCH_TURNS,
    ) -> None:
        self._client = client
        self._library = library
        self._fs_tools = fs_tools
        self._max_turns = max_turns

    def research(
        self, question: str, focus_paths: list[str] | None = None
    ) -> str:
        question = (question or "").strip()
        if not question:
            return "ERROR: research_codebase called with an empty question."

        focus = ", ".join(focus_paths) if focus_paths else "(none provided; whole repo is in scope)"
        prompt = render(
            self._library.task("research_codebase"),
            role_prompt=self._library.role("researcher"),
            question=question,
            focus_paths=focus,
        )

        try:
            result = self._client.run_agent(
                prompt,
                # Inner loop has base tools only. Recursion here would spawn a
                # nested researcher for every research question and burn cost
                # without bound; keeping the inner tool list to fs tools makes
                # that impossible by construction.
                tool_specs=list(_BASE_TOOL_SPECS),
                dispatch=self._fs_tools.dispatch,
                max_turns=self._max_turns,
                label=f"research:{question[:40]}",
            )
        except Exception as exc:  # noqa: BLE001 - a failed research call is data
            log.warning("research_codebase failed: %s", exc)
            return f"ERROR: research call failed: {exc}"

        if result.turn_limit_reached:
            return (
                f"ERROR: research hit the {self._max_turns}-turn limit without a "
                "conclusion. Try a narrower question."
            )
        answer = (result.final_output or "").strip()
        if not answer:
            return "ERROR: research returned no answer."
        return answer[:TOOL_OUTPUT_CHAR_LIMIT]


def build_dispatch(
    fs_tools: FileSystemTools,
    researcher: Researcher,
    *,
    max_calls: int = DEFAULT_RESEARCH_CALLS,
) -> Callable[[str, dict[str, Any]], str]:
    """Compose the base fs dispatch with budgeted ``research_codebase`` routing.

    The returned closure owns the budget, so callers get a fresh allowance by
    building a new dispatch — which is why this is a factory rather than a
    bound method. Build one per review; sharing a single dispatch across files
    would let the first large file spend the budget for all of them.

    Exhausting the budget returns a message, not an error: the reviewer can
    still finish with what it already read, and a review that concludes on
    partial evidence beats one that fails outright.
    """
    used = 0

    def dispatch(name: str, args: dict[str, Any]) -> str:
        nonlocal used
        if name != "research_codebase":
            return fs_tools.dispatch(name, args)
        if used >= max_calls:
            log.info("research budget of %d call(s) exhausted", max_calls)
            return (
                f"ERROR: the research budget for this review ({max_calls} calls) "
                "is spent. Conclude using the evidence you already have, and say "
                "what remains unverified rather than guessing."
            )
        used += 1
        question = args.get("question", "")
        focus = args.get("focus_paths") or None
        return researcher.research(question, focus_paths=focus)

    return dispatch


__all__ = [
    "DEFAULT_RESEARCH_CALLS",
    "DEFAULT_RESEARCH_TURNS",
    "RESEARCH_TOOL_SPEC",
    "Researcher",
    "build_dispatch",
    "extended_tool_specs",
]
