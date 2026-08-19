"""Provider adapter contract.

The review pipeline calls a :class:`ProviderClient`; only the factory knows
which concrete adapter it is. Adding a model means writing one new module and
extending the factory — the pipeline itself never grows a branch.

The three call shapes cover the whole pipeline:

``complete_text``          free-text answer, parsed by a last-line contract
``complete_structured``    strict function-call output validated into a Pydantic
                           model
``run_agent_structured``   tool-using exploration that ends by calling a strict
                           result tool
``run_agent``              read-only tool loop that ends in prose (agentic
                           validator)

The protocol is structural (:class:`typing.Protocol`), so a concrete adapter
does not need to inherit from anything — matching the signatures is enough.
"""

from __future__ import annotations

from typing import Any, Callable, Protocol, TypeVar, runtime_checkable

from pydantic import BaseModel

from .. import constants

M = TypeVar("M", bound=BaseModel)


@runtime_checkable
class ProviderClient(Protocol):
    """The pipeline's view of an LLM provider.

    Concrete adapters live in sibling modules (``deepseek.py`` today, plus
    whatever gets added later). Callers construct via :func:`make_client` and
    type-annotate against this protocol so a swap is a one-line change.
    """

    def complete_text(
        self,
        prompt: str,
        *,
        temperature: float | None = None,
        label: str = "complete_text",
    ) -> str: ...

    def complete_structured(
        self,
        prompt: str,
        schema: type[M],
        *,
        tool_name: str,
        tool_description: str,
        temperature: float | None = None,
        label: str = "complete_structured",
    ) -> M: ...

    def run_agent_structured(
        self,
        prompt: str,
        schema: type[M],
        *,
        tool_specs: list[dict[str, Any]],
        dispatch: Callable[[str, dict[str, Any]], str],
        result_tool: str,
        result_description: str,
        max_turns: int = constants.MAX_TURNS_REVIEW,
        temperature: float | None = None,
        label: str = "agent_structured",
    ) -> M: ...

    def run_agent(
        self,
        prompt: str,
        *,
        tool_specs: list[dict[str, Any]],
        dispatch: Callable[[str, dict[str, Any]], str],
        max_turns: int = constants.MAX_TURNS_VALIDATE,
        temperature: float | None = None,
        label: str = "agent",
    ) -> Any: ...
