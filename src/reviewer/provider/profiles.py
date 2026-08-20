"""What a given endpoint can actually do.

Every server behind this pipeline speaks the OpenAI chat-completions shape, and
that is where the similarity ends. DeepSeek needs a vendor field to turn
reasoning off and offers grammar-enforced output only through strict *function*
schemas on a beta endpoint; vLLM enforces a schema through `guided_json`;
llama.cpp and most quantised local servers enforce nothing at all and will
happily ignore a constraint they do not recognise.

Ignoring is the dangerous case. A server that rejects an unknown field fails
loudly and gets fixed in a minute. A server that accepts `strict: true` and
disregards it returns fluent JSON in a schema of its own invention — which is
exactly how this pipeline lost an afternoon to DeepSeek's `$defs` handling.
Stating the capability up front means the client picks an enforcement strategy
it knows the endpoint honours, and falls back to parsing rather than trusting.

Context window lives here too: chunking is a formality at a million tokens and
load-bearing at eight thousand, and that is a property of the endpoint, not of
the reviewer.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

# How the endpoint can be made to return a schema-shaped object.
#
# strict_tool  — publish the schema as a function and force the call
#                (DeepSeek /beta; grammar-enforced)
# json_schema  — response_format={"type": "json_schema", ...}
#                (OpenAI, and vLLM in OpenAI-compat mode)
# guided_json  — extra_body={"guided_json": schema} (vLLM native)
# json_object  — response_format={"type": "json_object"}: valid JSON, no schema
# prompt       — nothing enforced; the schema goes in the prompt and the
#                decode-side defences do the work
Enforcement = Literal["strict_tool", "json_schema", "guided_json", "json_object", "prompt"]


@dataclass(frozen=True)
class ProviderProfile:
    """Capabilities of one endpoint, named so a swap is a flag, not a patch."""

    name: str
    base_url: str
    default_model: str
    enforcement: Enforcement
    context_tokens: int
    # Vendor fields sent on every request. Empty for anything that might reject
    # an unrecognised key.
    extra_body: dict[str, Any] = field(default_factory=dict)
    # Whether the endpoint honours `tools`/`tool_calls` at all. A local server
    # without it cannot run the agentic reviewer or the validator, and the
    # pipeline degrades to diff-only review rather than looping uselessly.
    supports_tools: bool = True
    notes: str = ""

    @property
    def chunk_budget(self) -> int:
        """Leave room for the rules, the response, and a few tool round-trips.

        Two thirds of the window is the diff's share. The rule packs and task
        template run 6-10k tokens, the response a few thousand, and an agentic
        turn re-sends everything each time — a budget that only accounts for
        the diff overflows on the second tool call.
        """
        return max(int(self.context_tokens * 0.66), 2_000)


PROFILES: dict[str, ProviderProfile] = {
    "deepseek": ProviderProfile(
        name="deepseek",
        base_url="https://api.deepseek.com/beta",
        default_model="deepseek-v4-pro",
        enforcement="strict_tool",
        context_tokens=1_000_000,
        # v4-pro reasons by default, and a reasoning turn rejects a forced
        # tool_choice — which is how this profile gets its schema.
        extra_body={"thinking": {"type": "disabled"}},
        notes="Strict function schemas require the /beta endpoint.",
    ),
    "openai": ProviderProfile(
        name="openai",
        base_url="https://api.openai.com/v1",
        default_model="gpt-4.1",
        enforcement="json_schema",
        context_tokens=128_000,
    ),
    "vllm": ProviderProfile(
        name="vllm",
        base_url="http://localhost:8000/v1",
        default_model="local",
        enforcement="guided_json",
        context_tokens=32_768,
        notes="Set context_tokens to whatever --max-model-len the server was "
              "started with; the default here is vLLM's, not your model's.",
    ),
    "ollama": ProviderProfile(
        name="ollama",
        base_url="http://localhost:11434/v1",
        default_model="local",
        enforcement="json_schema",
        context_tokens=8_192,
        notes="num_ctx defaults low; a quantised model served with the stock "
              "context will truncate a large diff silently.",
    ),
    "llamacpp": ProviderProfile(
        name="llamacpp",
        base_url="http://localhost:8080/v1",
        default_model="local",
        enforcement="json_object",
        context_tokens=8_192,
        notes="llama-server enforces grammars natively but not through the "
              "OpenAI-compatible route; json_object plus the decode-side "
              "defences is the portable choice.",
    ),
    # The honest default for an unknown local server: assume nothing, verify
    # everything on the way back in.
    "generic": ProviderProfile(
        name="generic",
        base_url="http://localhost:8000/v1",
        default_model="local",
        enforcement="prompt",
        context_tokens=16_384,
        notes="Enforces nothing. Correctness rests entirely on the decode-side "
              "recovery in json_repair.",
    ),
}


def resolve(name: str) -> ProviderProfile:
    if name not in PROFILES:
        known = ", ".join(sorted(PROFILES))
        raise ValueError(f"unknown provider profile {name!r}; known: {known}")
    return PROFILES[name]


__all__ = ["Enforcement", "PROFILES", "ProviderProfile", "resolve"]
