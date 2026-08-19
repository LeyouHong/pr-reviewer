"""Offline checks: no API calls, no network. Exercises every pure component."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from reviewer.config import Config
from reviewer.diffing.chunker import chunk_file_change
from reviewer.diffing.parser import parse_unified_diff, render_diff, snippet_for_lines
from reviewer.diffing.scope import filter_in_scope, scope_section
from reviewer.models import (
    Category, Complexity, LineNumber, LineState, LLMFileChangeReview,
    Maintainability, Metrics, OverallRating, ReviewComment, Severity, TestCoverage,
)
from reviewer.pipeline.qualify import heuristic_verdict
from reviewer.policy import PolicyRouter
from reviewer.prompt import PromptLibrary, file_changes_block, render
from reviewer.provider.json_repair import clamp_and_revalidate, loads_with_recovery
from reviewer.provider.strict_schema import build_strict_tool
from reviewer.models import QualifyVerdict

FAILURES = []

def check(name, cond, detail=""):
    if cond:
        print(f"  PASS  {name}")
    else:
        print(f"  FAIL  {name} {detail}")
        FAILURES.append(name)

DIFF = """diff --git a/src/api/orders.py b/src/api/orders.py
index 1111111..2222222 100644
--- a/src/api/orders.py
+++ b/src/api/orders.py
@@ -10,7 +10,11 @@ def load(order_id):
     record = repo.find(order_id)
     return record
 
-def total(items):
-    return sum(i.price for i in items)
+def total(items, discounts=[]):
+    subtotal = sum(i.price for i in items)
+    for d in discounts:
+        subtotal -= d
+    return subtotal
 
 def close(order):
     order.status = "closed"
"""

print("\n[1] diff parsing")
files = parse_unified_diff(DIFF)
check("one file parsed", len(files) == 1, f"got {len(files)}")
fc = files[0]
check("filepath", fc.filepath == "src/api/orders.py", fc.filepath)
check("added lines found", fc.added_lines == {13, 14, 15, 16, 17}, sorted(fc.added_lines))
check("removed lines found", len(fc.removed_lines) >= 2, sorted(fc.removed_lines))

print("\n[1b] patch-series detection")
import io
import logging

_parser_log = logging.getLogger("reviewer.diffing.parser")

def _warnings_from(diff_text):
    buf = io.StringIO()
    handler = logging.StreamHandler(buf)
    handler.setLevel(logging.WARNING)
    _parser_log.addHandler(handler)
    try:
        parsed = parse_unified_diff(diff_text)
    finally:
        _parser_log.removeHandler(handler)
    return parsed, buf.getvalue()

_combined, _clean_log = _warnings_from(DIFF)
_series, _dirty_log = _warnings_from(DIFF + DIFF)  # what `gh pr diff --patch` emits
check("combined diff produces no warning", "more than once" not in _clean_log, _clean_log[:80])
check("duplicate file entries surface a warning", "more than once" in _dirty_log, _dirty_log[:80])
check("duplicate entries are not silently merged", len(_series) == 2, len(_series))

print("\n[2] gutter rendering")
rendered = render_diff(fc)
check("gutter has old|new columns", " | " in rendered)
check("hunk header has blank gutter", "     | " in rendered.splitlines()[0] or rendered.splitlines()[0].startswith("       |"))
check("added line marked", any(l.rstrip().endswith('def total(items, discounts=[]):') and "+" in l for l in rendered.splitlines()))

print("\n[3] scope filtering")
def mk(line, state=LineState.DIFF_ADDED, sev=Severity.ERROR, msg="mutable default arg", sugg="use None sentinel"):
    return ReviewComment(
        line_numbers=[LineNumber(line_number=line, line_number_state=state)],
        severity=sev, category=Category.LOGIC, message=msg, criteria="python-mutable-default",
        suggestion=sugg, rule="python-mutable-default",
        implementation_complexity=Complexity.LOW, context_needed=False,
    )
kept, dropped = filter_in_scope(fc, [mk(13), mk(9999), mk(11, LineState.FILE_CONTEXT)])
check("real added citation kept", len(kept) == 1 and kept[0].line_numbers[0].line_number == 13)
check("fabricated citation dropped", any(c.line_numbers[0].line_number == 9999 for c in dropped))
check("context-only citation dropped as out of scope", len(dropped) == 2, [c.line_numbers[0].line_number for c in dropped])

print("\n[4] scope_section text")
sec = scope_section(fc, mk(13))
check("names added ranges", "13-17" in sec, sec)
check("reports cited-inside", "Cited lines that this diff added: [13]" in sec, sec)

print("\n[5] qualification heuristics (real regex set)")
check("info auto-passes", heuristic_verdict(mk(13, sev=Severity.INFO), fc) is QualifyVerdict.PASS)
check("out-of-diff discarded", heuristic_verdict(mk(9999), fc) is QualifyVerdict.DISCARD)

narration = mk(13, msg="The previous code omitted the guard; the fix correctly handles it.", sugg="No change needed.")
check("fix-narration discarded", heuristic_verdict(narration, fc) is QualifyVerdict.DISCARD)
contrastive = mk(13, msg="The fix correctly handles the guard, however the loop still mutates the shared default.")
check("contrastive clause routes to validate, not discard", heuristic_verdict(contrastive, fc) is QualifyVerdict.VALIDATE)
in_sugg = mk(13, msg="Discount loop rewritten.", sugg="The fix is correct as written.")
check("narration in the suggestion also caught", heuristic_verdict(in_sugg, fc) is QualifyVerdict.DISCARD)

restate = mk(13, msg="use None sentinel instead", sugg="use None sentinel")
check("restated suggestion discarded", heuristic_verdict(restate, fc) is QualifyVerdict.DISCARD)
near = mk(13, msg="The default list is shared.", sugg="Default to None and build a fresh list inside the function body.")
check("genuinely different suggestion survives overlap test", heuristic_verdict(near, fc) is None)

uncertain = mk(13, msg="This might break callers of total() elsewhere.")
check("uncertainty with verb complement routes to validate", heuristic_verdict(uncertain, fc) is QualifyVerdict.VALIDATE)
bare_may = mk(13, msg="This may be a security issue if the token is guessable.", sugg="Rotate the token.")
check("bare 'may' does NOT force validation", heuristic_verdict(bare_may, fc) is None, "tightened: needs a verb complement")
cross = mk(13, msg="Callers of total() assume two arguments.", sugg="Update the signature.")
check("cross-file claim routes to validate", heuristic_verdict(cross, fc) is QualifyVerdict.VALIDATE)

prev_only = mk(13, msg="The previous code lacked a guard here.", sugg="Add a guard for the empty case.")
check("'previous code' alone no longer auto-discards", heuristic_verdict(prev_only, fc) is None, "narrow patterns only")
clear = mk(13, msg="Mutable default argument is shared across calls.", sugg="Default to None and build the list inside.")
check("clear defect needs the LLM gate", heuristic_verdict(clear, fc) is None)

print("\n[6] strict schema for DeepSeek")
tool = build_strict_tool(LLMFileChangeReview, "submit_file_review", "desc")
fn = tool["function"]
check("strict flag set", fn["strict"] is True)
params = fn["parameters"]
check("top-level additionalProperties false", params["additionalProperties"] is False)
check("top-level required complete", set(params["required"]) == {"overall_rating", "summary", "comments", "metrics"}, params["required"])
comment_def = params["$defs"]["ReviewComment"]
check("nested required includes optional field", "implementation_notes" in comment_def["required"], comment_def["required"])
check("nested additionalProperties false", comment_def["additionalProperties"] is False)
check("optional stays nullable", any(o.get("type") == "null" for o in comment_def["properties"]["implementation_notes"].get("anyOf", [])))
def scan(node):
    if isinstance(node, dict):
        if node.get("type") == "object" and "properties" in node:
            if node.get("additionalProperties") is not False: return False
            if set(node.get("required", [])) != set(node["properties"]): return False
        return all(scan(v) for v in node.values())
    if isinstance(node, list):
        return all(scan(v) for v in node)
    return True
check("every object node hardened", scan(params))

print("\n[7] json repair")
check("fenced json recovered", loads_with_recovery('```json\n{"a": 1}\n```')["a"] == 1)
check("prose-wrapped json recovered", loads_with_recovery('Here you go:\n{"a": 2}\nDone.')["a"] == 2)
from pydantic import BaseModel, Field
class Tiny(BaseModel):
    text: str = Field(max_length=10)
check("clamp truncates over-long field", len(clamp_and_revalidate({"text": "x" * 50}, Tiny).text) == 10)

print("\n[8] policy routing")
res = Path(__file__).resolve().parents[1] / "resources"
router = PolicyRouter(res)
for path, want in [("src/api/orders.py", "python"), ("web/App.tsx", "typescript"),
                   ("src/Main.java", "java"), ("cmd/server/main.go", "go")]:
    m = router.match(path)
    check(f"{path} -> {want}", want in m.names and "general" in m.names, m.names)
check("vendor excluded", router.excluded("vendor/foo/bar.go"))
check("normal path not excluded", not router.excluded("src/api/orders.py"))

print("\n[9] prompt composition")
lib = PromptLibrary(res)
check("reviewer role loaded", lib.role("reviewer") == "You are a code review assistant.")
tmpl = lib.task("code_review_prompt")
prompt = render(
    tmpl, role_prompt=lib.role("reviewer"), mr_project="acme/api", mr_title="Add discounts",
    mr_source_branch="feat/x", mr_target_branch="main", review_date="2026-08-19",
    mr_description="desc", mr_file_changes=file_changes_block(fc.filepath, rendered),
    general_rules=lib.rules("/rules/general.md"), rules=lib.rules("/rules/language_rules/python.md"),
)
check("no unsubstituted known placeholders", "{role_prompt}" not in prompt and "{rules}" not in prompt and "{general_rules}" not in prompt)
check("rules actually embedded", "python-mutable-default" in prompt and "general-noise" in prompt)
check("diff embedded", "def total(items, discounts=[])" in prompt)
check("missing resource is loud", "[Missing prompt resource" in lib.rules("/rules/does_not_exist.md"))

print("\n[10] literal braces survive rendering (the str.format trap)")
tricky = 'Return {"a": 1} and use {name} plus {{escaped}}'
out = render(tricky, name="VALUE")
check("json braces untouched", '{"a": 1}' in out, out)
check("known key substituted", "VALUE" in out, out)
check("unknown braces left alone", "{{escaped}}" in out, out)

print("\n[11] snippet extraction + chunking")
snip = snippet_for_lines(fc, {13})
check("snippet contains cited line", "discounts=[]" in snip)
chunks = chunk_file_change(fc)
check("small file is one chunk", len(chunks) == 1, len(chunks))
tiny_chunks = chunk_file_change(fc, budget=30)
check("tiny budget forces a split", len(tiny_chunks) >= 2, len(tiny_chunks))

print("\n[12] model round-trip")
review = LLMFileChangeReview(
    overall_rating=OverallRating.NEEDS_IMPROVEMENT, summary="s", comments=[mk(13)],
    metrics=Metrics(complexity=Complexity.LOW, test_coverage=TestCoverage.NONE, maintainability=Maintainability.MEDIUM),
)
round_tripped = LLMFileChangeReview.model_validate(json.loads(review.model_dump_json()))
check("review serialises and validates", round_tripped.comments[0].line_numbers[0].line_number == 13)

print()
if FAILURES:
    print(f"{len(FAILURES)} FAILED: {FAILURES}")
    sys.exit(1)
print("all offline checks passed")
