"""Live checks against the real DeepSeek endpoint. Costs a few cents.

Offline tests cannot see any of this: both real defects found so far — thinking
mode rejecting a forced tool_choice, and strict silently degrading when the
schema kept Pydantic's $defs indirection — passed every offline assertion and
failed on the first live call. Run this after any provider-layer change.

    env $(grep -v '^#' .env | xargs) PYTHONPATH=src python tests/smoke_live.py
"""
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
logging.basicConfig(level=logging.ERROR)

from reviewer.config import Config
from reviewer.models import Category, LineState, LLMFileChangeReview, Severity
from reviewer.provider import DeepSeekClient
from reviewer.tools.fs_tools import TOOL_SPECS, FileSystemTools

FAILURES = []


def check(name, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + name + (f"  {detail}" if not cond else ""))
    if not cond:
        FAILURES.append(name)


PROMPT = """You are a code review assistant. Review this Python diff and report via the function.

     1 |      1 +def total(items, discounts=[]):
     2 |      2 +    return sum(i.price for i in items) - sum(discounts)

Rule: a mutable default argument is shared across calls and MUST be flagged at
error severity, category logic. Cite line 1 with line_number_state diff-added."""

cfg = Config.from_env()
client = DeepSeekClient(cfg)
print(f"model={cfg.model} base_url={cfg.base_url} thinking={cfg.thinking}\n")

print("[1] structured output via strict function schema")
review = client.complete_structured(
    PROMPT, LLMFileChangeReview,
    tool_name="submit_file_review", tool_description="Submit the file review.",
    label="smoke-structured",
)
check("enum honoured (overall_rating)", review.overall_rating.value in
      {"excellent", "good", "needs_improvement", "poor"}, review.overall_rating)
check("nested array field present (line_numbers)",
      all(c.line_numbers for c in review.comments) if review.comments else True)
check("nested enum honoured (line_number_state)",
      all(isinstance(l.line_number_state, LineState)
          for c in review.comments for l in c.line_numbers))
check("nested object honoured (metrics)", review.metrics is not None)
check("found the defect", any(c.severity is Severity.ERROR for c in review.comments),
      [c.severity.value for c in review.comments])
check("category is from our enum",
      all(isinstance(c.category, Category) for c in review.comments))
for c in review.comments:
    print(f"        [{c.severity.value}/{c.category.value}] "
          f"L{[l.line_number for l in c.line_numbers]} {c.message[:60]}")

print("\n[2] plain-text completion with a last-line contract")
text = client.complete_text(
    "Reply with a one-sentence reason, then put only the word `validate` on the "
    "last line, with no formatting.", label="smoke-text")
check("non-empty response", bool(text.strip()))
check("last line is the bare keyword", text.strip().splitlines()[-1].strip().lower()
      == "validate", repr(text.strip().splitlines()[-1]))

print("\n[3] agentic tool loop")
tools = FileSystemTools(Path(__file__).resolve().parents[1])
result = client.run_agent(
    "Use list_directory on 'resources/role/default' and then read_file on "
    "'resources/role/default/reviewer.md'. Then state on the last line exactly: "
    "Verdict: TRUE_POSITIVE",
    tool_specs=TOOL_SPECS, dispatch=tools.dispatch, max_turns=6, label="smoke-agent")
check("model called tools", result.used_tools,
      [e.name for e in result.events if e.kind == "tool_call"])
check("did not hit the turn limit", not result.turn_limit_reached, result.turns_used)
check("last-line verdict contract", "Verdict: TRUE_POSITIVE" in result.final_output,
      result.final_output[-80:])
print(f"        tools called: {[e.name for e in result.events if e.kind=='tool_call']}"
      f"  turns={result.turns_used}")

print("\n[4] agentic reviewer: explore, then end on the result tool")
from reviewer.models import LLMFileChangeReview as _R
root = Path(__file__).resolve().parents[1]
agent_tools = FileSystemTools(root)
_calls = []


def _recording_dispatch(name, args):
    _calls.append(name)
    return agent_tools.dispatch(name, args)

review2 = client.run_agent_structured(
    "You are a code review assistant. Determine which severity values the "
    "reviewer schema allows by reading `resources/rules/general.md` in this "
    "repository, then submit a review of the diff below.\n\n"
    "````file-diff path=/x.py\n"
    "       |      1 +def total(items, discounts=[]):\n"
    "       |      2 +    return sum(i.price for i in items)\n"
    "````\n\n"
    "Report one error-severity finding on line 1 for the mutable default.",
    _R, tool_specs=TOOL_SPECS, dispatch=_recording_dispatch,
    result_tool="submit_file_review", result_description="Submit the review.",
    max_turns=10, label="smoke-agentic-review")
check("read the repository before answering", bool(_calls), _calls)
check("actually opened a file", "read_file" in _calls, _calls)
check("ended on the structured result tool", isinstance(review2, _R))
check("schema honoured through the agentic path",
      review2.overall_rating.value in {"excellent","good","needs_improvement","poor"})
check("produced a finding", len(review2.comments) >= 1, len(review2.comments))
for c in review2.comments[:2]:
    print(f"        [{c.severity.value}] L{[l.line_number for l in c.line_numbers]} {c.message[:60]}")
print(f"        tools called: {_calls}")

print()
if FAILURES:
    print(f"{len(FAILURES)} FAILED: {FAILURES}")
    sys.exit(1)
print("all live checks passed")
