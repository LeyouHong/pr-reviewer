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

The schema is also fully inlined: every ``$ref`` is replaced by the definition
it points at and ``$defs`` is dropped. A live check against ``deepseek-v4-pro``
showed the model inventing its own field names whenever the schema kept
Pydantic's ``$defs`` indirection — the nested definitions never reached the
grammar, so ``strict`` silently degraded to a hint. A self-contained schema
removes the dependency on how any given provider spells or resolves the
definitions section.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel


_MAX_INLINE_DEPTH = 12


def _inline_refs(node: Any, defs: dict[str, Any], depth: int = 0) -> Any:
    """Replace every ``$ref`` with the definition it names, recursively.

    ``depth`` bounds recursion so a self-referential model degrades into a
    truncated schema instead of recursing forever. None of the reviewer models
    are cyclic; the guard is there so a future one fails visibly.
    """
    if isinstance(node, list):
        return [_inline_refs(item, defs, depth) for item in node]
    if not isinstance(node, dict):
        return node

    ref = node.get("$ref")
    if isinstance(ref, str) and ref.startswith("#/$defs/"):
        name = ref.split("/")[-1]
        target = defs.get(name)
        if target is None:
            raise ValueError(f"schema references unknown definition {name!r}")
        if depth >= _MAX_INLINE_DEPTH:
            raise ValueError(
                f"schema nesting exceeded {_MAX_INLINE_DEPTH} levels at {name!r}; "
                "the model is probably self-referential"
            )
        merged = _inline_refs(target, defs, depth + 1)
        # Keep sibling keys (description, default, ...) that sat next to $ref.
        extras = {k: v for k, v in node.items() if k != "$ref"}
        return {**merged, **_inline_refs(extras, defs, depth)} if extras else merged

    return {k: _inline_refs(v, defs, depth) for k, v in node.items() if k != "$defs"}


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


def harden_schema(model: type[BaseModel]) -> dict[str, Any]:
    """The inlined, strict-shaped JSON schema for ``model``.

    Shared by every enforcement strategy: a response_format schema, a vLLM
    guided_json blob, and the block pasted into a prompt all want the same
    self-contained document.
    """
    schema = model.model_json_schema()
    defs = schema.pop("$defs", {})
    schema = _inline_refs(schema, defs)
    schema.pop("title", None)
    _harden(schema)
    return schema


def build_strict_tool(
    model: type[BaseModel],
    name: str,
    description: str,
) -> dict[str, Any]:
    """Publish ``model`` as a strict function tool definition."""
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "strict": True,
            "parameters": harden_schema(model),
        },
    }
