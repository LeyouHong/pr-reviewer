"""DeepSeek adapter.

Three call shapes cover the whole pipeline:

``complete_text``        free-text answer, parsed by a last-line contract
``complete_structured``  strict function-call output validated into a Pydantic model
``run_agent``            manual tool loop for the agentic validator

Everything funnels through :func:`provider.errors.with_retries`, so no caller
implements its own backoff.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Callable, TypeVar

from openai import OpenAI
from pydantic import BaseModel

from .. import constants
from ..config import Config
from .errors import ContentError, DegenerateOutputError, with_retries
from .json_repair import clamp_and_revalidate, loads_with_recovery
from .strict_schema import build_strict_tool

log = logging.getLogger(__name__)

M = TypeVar("M", bound=BaseModel)


@dataclass
class AgentEvent:
    kind: str  # "tool_call" | "tool_output" | "message"
    name: str = ""
    payload: str = ""


@dataclass
class AgentRunResult:
    final_output: str
    events: list[AgentEvent] = field(default_factory=list)
    turns_used: int = 0
    turn_limit_reached: bool = False

    @property
    def used_tools(self) -> bool:
        return any(e.kind == "tool_call" for e in self.events)


class DeepSeekClient:
    def __init__(self, config: Config):
        self._config = config
        self._client = OpenAI(
            api_key=config.require_api_key(),
            base_url=config.base_url,
            timeout=600.0,
            max_retries=0,  # retries are owned by provider.errors
        )

    # -- plain text -------------------------------------------------------

    def complete_text(
        self,
        prompt: str,
        *,
        temperature: float | None = None,
        label: str = "complete_text",
    ) -> str:
        def _call() -> str:
            resp = self._client.chat.completions.create(
                model=self._config.model,
                messages=[{"role": "user", "content": prompt}],
                temperature=(
                    self._config.temperature if temperature is None else temperature
                ),
            )
            content = (resp.choices[0].message.content or "").strip()
            if not content:
                raise DegenerateOutputError(f"{label}: empty content from model")
            return content

        return with_retries(_call, label=label)

    # -- structured output ------------------------------------------------

    def complete_structured(
        self,
        prompt: str,
        schema: type[M],
        *,
        tool_name: str,
        tool_description: str,
        temperature: float | None = None,
        label: str = "complete_structured",
    ) -> M:
        """Force a strict function call and validate its arguments.

        Strict function schemas are the only grammar-enforced output surface
        DeepSeek offers, so the result model is published as a tool and the
        model is required to call it via ``tool_choice``.
        """
        tool = build_strict_tool(schema, tool_name, tool_description)

        def _call() -> M:
            resp = self._client.chat.completions.create(
                model=self._config.model,
                messages=[{"role": "user", "content": prompt}],
                tools=[tool],
                tool_choice={"type": "function", "function": {"name": tool_name}},
                temperature=(
                    self._config.temperature if temperature is None else temperature
                ),
            )
            message = resp.choices[0].message
            calls = message.tool_calls or []
            if not calls:
                # Fall back to whatever prose came back, in case the model
                # answered inline despite tool_choice.
                if message.content:
                    payload = loads_with_recovery(message.content)
                    return clamp_and_revalidate(payload, schema)
                raise DegenerateOutputError(f"{label}: no tool call and no content")

            raw = calls[0].function.arguments or ""
            if not raw.strip():
                raise DegenerateOutputError(f"{label}: empty tool arguments")
            try:
                payload = loads_with_recovery(raw)
            except json.JSONDecodeError as exc:
                raise ContentError(f"{label}: unparseable tool arguments") from exc
            return clamp_and_revalidate(payload, schema)

        return with_retries(_call, label=label)

    # -- agentic loop -----------------------------------------------------

    def run_agent(
        self,
        prompt: str,
        *,
        tool_specs: list[dict[str, Any]],
        dispatch: Callable[[str, dict[str, Any]], str],
        max_turns: int = constants.MAX_TURNS_VALIDATE,
        temperature: float | None = None,
        label: str = "agent",
    ) -> AgentRunResult:
        """Drive a read-only tool loop until the model answers in prose."""
        messages: list[dict[str, Any]] = [{"role": "user", "content": prompt}]
        events: list[AgentEvent] = []
        temp = self._config.temperature if temperature is None else temperature

        for turn in range(1, max_turns + 1):

            def _call() -> Any:
                return self._client.chat.completions.create(
                    model=self._config.model,
                    messages=messages,
                    tools=tool_specs,
                    temperature=temp,
                )

            resp = with_retries(_call, label=f"{label}:turn{turn}")
            message = resp.choices[0].message
            calls = message.tool_calls or []

            if not calls:
                text = (message.content or "").strip()
                if not text:
                    raise DegenerateOutputError(f"{label}: empty final message")
                events.append(AgentEvent(kind="message", payload=text))
                return AgentRunResult(
                    final_output=text, events=events, turns_used=turn
                )

            messages.append(
                {
                    "role": "assistant",
                    "content": message.content or "",
                    "tool_calls": [
                        {
                            "id": c.id,
                            "type": "function",
                            "function": {
                                "name": c.function.name,
                                "arguments": c.function.arguments,
                            },
                        }
                        for c in calls
                    ],
                }
            )

            for call in calls:
                name = call.function.name
                try:
                    args = json.loads(call.function.arguments or "{}")
                except json.JSONDecodeError:
                    args = {}
                events.append(
                    AgentEvent(kind="tool_call", name=name, payload=json.dumps(args))
                )
                try:
                    output = dispatch(name, args)
                except Exception as exc:  # tool errors are data, not failures
                    output = f"ERROR: {exc}"
                output = output[: constants.TOOL_OUTPUT_CHAR_LIMIT]
                events.append(AgentEvent(kind="tool_output", name=name, payload=output))
                messages.append(
                    {"role": "tool", "tool_call_id": call.id, "content": output}
                )

        log.warning("%s: hit turn limit (%d)", label, max_turns)
        return AgentRunResult(
            final_output="",
            events=events,
            turns_used=max_turns,
            turn_limit_reached=True,
        )
