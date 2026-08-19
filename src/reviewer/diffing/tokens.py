"""Token counting for chunk budgeting.

DeepSeek ships its own tokenizer; ``tiktoken`` would misreport here, so it is
deliberately not used. If ``transformers`` is installed and a local DeepSeek
tokenizer is configured, that is preferred; otherwise a character-ratio
estimate is used.

The estimate treats special-token literals appearing in *source code* as
ordinary text — counting them as tokens is what blew the context budget on
legitimate files in the reference implementation.
"""

from __future__ import annotations

import os
from functools import lru_cache

# Empirically ~3.2 chars/token for mixed source code; conservative on purpose.
_CHARS_PER_TOKEN = 3.2


@lru_cache(maxsize=1)
def _hf_tokenizer():
    path = os.environ.get("DEEPSEEK_TOKENIZER_PATH")
    if not path:
        return None
    try:
        from transformers import AutoTokenizer  # type: ignore

        return AutoTokenizer.from_pretrained(path, trust_remote_code=True)
    except Exception:
        return None


def count_tokens(text: str) -> int:
    tok = _hf_tokenizer()
    if tok is not None:
        try:
            return len(tok.encode(text, add_special_tokens=False))
        except Exception:
            pass
    return int(len(text) / _CHARS_PER_TOKEN) + 1
