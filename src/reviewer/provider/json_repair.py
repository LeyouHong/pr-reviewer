"""Decode-side defences for model-produced JSON.

Layer order, cheapest first:
  1. plain ``json.loads``
  2. fence stripping — models wrap JSON in triple backticks even when told not to
  3. outermost-brace extraction — strips prose preamble/postamble
  4. clamp-and-accept — truncate over-length string fields using the
     ``max_length`` reported by the validation error, then re-validate
"""

from __future__ import annotations

import json
import re
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError

M = TypeVar("M", bound=BaseModel)

_FENCE = re.compile(r"^\s*```(?:json|JSON)?\s*|\s*```\s*$")


def strip_fences(text: str) -> str:
    out = _FENCE.sub("", text.strip())
    return out.strip()


def _extract_braces(text: str) -> str | None:
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None
    return text[start : end + 1]


def loads_with_recovery(text: str) -> Any:
    """Parse JSON from a model response, tolerating fences and stray prose."""
    for candidate in (text, strip_fences(text), _extract_braces(text) or ""):
        if not candidate:
            continue
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            continue
    raise json.JSONDecodeError("no parseable JSON in response", text[:200], 0)


def _truncate_at(payload: Any, loc: tuple, limit: int) -> bool:
    """Walk ``loc`` into ``payload`` and truncate the string it points at."""
    node = payload
    for key in loc[:-1]:
        try:
            node = node[key]
        except (KeyError, IndexError, TypeError):
            return False
    last = loc[-1]
    try:
        value = node[last]
    except (KeyError, IndexError, TypeError):
        return False
    if not isinstance(value, str) or len(value) <= limit:
        return False
    node[last] = value[: max(limit - 1, 0)] + "…"
    return True


def clamp_and_revalidate(payload: Any, model: type[M], max_passes: int = 6) -> M:
    """Validate ``payload``, shrinking over-length strings until it fits.

    Converges in at most one pass per nesting depth: each pass fixes every
    ``string_too_long`` error the validator currently reports, and truncation
    cannot introduce new ones.
    """
    for _ in range(max_passes):
        try:
            return model.model_validate(payload)
        except ValidationError as exc:
            repaired = False
            for err in exc.errors():
                if err.get("type") != "string_too_long":
                    continue
                limit = (err.get("ctx") or {}).get("max_length")
                if not isinstance(limit, int):
                    continue
                repaired |= _truncate_at(payload, tuple(err["loc"]), limit)
            if not repaired:
                raise
    return model.model_validate(payload)
