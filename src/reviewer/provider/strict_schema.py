"""Convert a Pydantic model into a DeepSeek *strict* function schema.

DeepSeek exposes no ``response_format={"type": "json_schema"}``; the only
grammar-enforced surface is strict **function** calling on the ``/beta``
endpoint. So the reviewer's output schema is published as a tool the model is
forced to call, which buys back the encode-time guarantee that plain
``json_object`` mode cannot give.

Strict mode requires, at every object level:
  * ``additionalProperties: false``
  * every declared property listed in ``required``

Pydantic marks fields with defaults as optional, so this module rewrites the
schema rather than trusting it. Optional fields keep their ``anyOf [..., null]``
shape, which strict mode accepts — the model must emit the key, but may emit
``null``.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel


def _harden(node: Any) -> None:
    """Recursively force strict-mode invariants onto a JSON-schema node."""
    if isinstance(node, list):
        for item in node:
            _harden(item)
        return
    if not isinstance(node, dict):
        return

    if node.get("type") == "object" or "properties" in node:
        props = node.get("properties")
        if isinstance(props, dict):
            node["additionalProperties"] = False
            node["required"] = list(props.keys())
            for sub in props.values():
                _harden(sub)

    for key in ("items", "prefixItems", "not"):
        if key in node:
            _harden(node[key])

    for key in ("anyOf", "allOf", "oneOf"):
        if key in node:
            _harden(node[key])

    defs = node.get("$defs")
    if isinstance(defs, dict):
        for sub in defs.values():
            _harden(sub)


def build_strict_tool(
    model: type[BaseModel],
    name: str,
    description: str,
) -> dict[str, Any]:
    """Publish ``model`` as a strict function tool definition."""
    schema = model.model_json_schema()
    # Pydantic emits a top-level "title"/"description"; harmless but noisy.
    schema.pop("title", None)
    _harden(schema)
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "strict": True,
            "parameters": schema,
        },
    }
