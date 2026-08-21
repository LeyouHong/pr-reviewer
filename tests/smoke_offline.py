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


def _raises(fn, exc_type):
    try:
        fn()
    except exc_type:
        return True
    except Exception:
        return False
    return False

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
# Info no longer bypasses the discard checks: a narrated fix is as useless at
# info severity as at error severity.
info_narration = mk(13, sev=Severity.INFO, msg="The fix correctly handles the shared default.", sugg="None needed.")
check("info fix-narration is discarded, not auto-passed", heuristic_verdict(info_narration, fc) is QualifyVerdict.DISCARD)
check("info outside the diff is discarded", heuristic_verdict(mk(9999, sev=Severity.INFO), fc) is QualifyVerdict.DISCARD)
info_uncertain = mk(13, sev=Severity.INFO, msg="This might break callers of total() elsewhere.", sugg="Check them.")
check("info never routes to agentic validation", heuristic_verdict(info_uncertain, fc) is None, "info must reach the value gate, not the validator")
warn_uncertain = mk(13, sev=Severity.WARNING, msg="This might break callers of total() elsewhere.", sugg="Check them.")
check("warning with the same text still routes to validate", heuristic_verdict(warn_uncertain, fc) is QualifyVerdict.VALIDATE)
info_plain = mk(13, sev=Severity.INFO, msg="Function lacks a docstring.", sugg="Add one describing the discount semantics.")
check("ordinary info defers to the value gate", heuristic_verdict(info_plain, fc) is None)
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
comment_def = params["properties"]["comments"]["items"]
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
# The live endpoint ignored nested definitions reached through $defs, so the
# schema must be self-contained before it is ever sent.
blob = json.dumps(params)
check("no $defs survive inlining", "$defs" not in blob)
check("no $ref survives inlining", "$ref" not in blob)
check("nested model inlined in place",
      sorted(params["properties"]["comments"]["items"]["properties"]) == [
          "category", "context_needed", "criteria", "implementation_complexity",
          "implementation_notes", "line_numbers", "message", "rule", "severity",
          "suggestion"])
check("doubly-nested model inlined",
      sorted(params["properties"]["comments"]["items"]["properties"]
             ["line_numbers"]["items"]["properties"]) == ["line_number", "line_number_state"])
from reviewer.provider.strict_schema import _inline_refs
try:
    _inline_refs({"$ref": "#/$defs/Nope"}, {})
    check("unknown $ref raises", False, "no error")
except ValueError:
    check("unknown $ref raises", True)

print("\n[6b] error classification")
from reviewer.provider.errors import ErrorKind, classify, is_billing_failure
cases = [
    ("Error code: 402 - {'message': 'Insufficient Balance'}", ErrorKind.FATAL, True),
    ("Error code: 429 - rate limit exceeded", ErrorKind.CAPACITY, False),
    ("Request timed out", ErrorKind.TRANSPORT, False),
    ("503 Service Unavailable", ErrorKind.TRANSPORT, False),
]
for text, want, billing in cases:
    got = classify(RuntimeError(text))
    check(f"{text[:34]!r} -> {want.value}", got is want, got.value)
    check(f"  billing flag == {billing}", is_billing_failure(RuntimeError(text)) is billing)

print("\n[6c] provider profiles: one flag swaps the endpoint")
from reviewer.provider.profiles import PROFILES, resolve
from reviewer.provider.client import DeepSeekClient, _with_schema
from reviewer.provider.strict_schema import harden_schema

class _Cfg(Config):
    pass

check("unknown profile is refused, not defaulted",
      _raises(lambda: resolve("gpt-9"), ValueError))
check("deepseek keeps its vendor field", PROFILES["deepseek"].extra_body == {"thinking": {"type": "disabled"}})
for name in ("vllm", "ollama", "llamacpp", "generic"):
    check(f"{name} sends no vendor fields", PROFILES[name].extra_body == {},
          PROFILES[name].extra_body)
check("a small window gets a small chunk budget",
      PROFILES["ollama"].chunk_budget < PROFILES["deepseek"].chunk_budget // 50)
check("chunk budget leaves room for rules and response",
      PROFILES["ollama"].chunk_budget < PROFILES["ollama"].context_tokens)

# The request body must differ per strategy — that is the whole point.
def _body(profile_name):
    cfg = Config.from_env(api_key="x", provider_profile=profile_name)
    client = DeepSeekClient.__new__(DeepSeekClient)
    client._config = cfg
    client._profile = resolve(profile_name)
    client._extra_body = dict(client._profile.extra_body)
    return client._structured_request("p", LLMFileChangeReview, "submit", "d")

b = _body("deepseek")
check("strict_tool forces the function call", b["tool_choice"]["function"]["name"] == "submit")
check("strict_tool carries the vendor field", "thinking" in b["extra_body"])
b = _body("openai")
check("json_schema uses response_format", b["response_format"]["type"] == "json_schema")
check("json_schema sends no tools", "tools" not in b)
b = _body("vllm")
check("guided_json goes in extra_body", "guided_json" in b["extra_body"])
check("guided_json sends no response_format", "response_format" not in b)
b = _body("llamacpp")
check("json_object asks only for valid json", b["response_format"]["type"] == "json_object")
check("json_object puts the schema in the prompt", "schema" in b["messages"][0]["content"].lower())
b = _body("generic")
check("prompt strategy constrains nothing", "response_format" not in b and "tools" not in b)
check("prompt strategy still states the schema",
      "Output the JSON object and nothing else" in b["messages"][0]["content"])

check("schema in the prompt is self-contained",
      "$defs" not in _with_schema("p", harden_schema(LLMFileChangeReview)))

# A local endpoint needs no key; a hosted one must not silently proceed without.
check("localhost needs no key",
      Config.from_env(provider_profile="ollama").require_api_key() == "local")
check("a hosted endpoint without a key fails loudly",
      _raises(lambda: Config(api_key="", base_url="https://api.example.com/v1").require_api_key(),
              SystemExit))

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

print("\n[8b] semgrep options + suppression")
from reviewer.policy import SemgrepSuppress, is_suppressed
opts = router.semgrep_options()
check("semgrep policy rules loaded", opts.enabled and len(opts.rules) >= 1, opts.rules)
check("semgrep suppress carries reason", all(s.reason for s in opts.suppress), opts.suppress)
suppress_case = SemgrepSuppress(
    path_prefixes=["scripts/"], rule_patterns=["python.subprocess.*"],
    reason="operator scripts intentionally shell out",
)
check("path + rule match suppresses",
      is_suppressed("scripts/deploy.py", "python.subprocess.foo", [suppress_case])
      == "operator scripts intentionally shell out")
check("path match but wrong rule not suppressed",
      is_suppressed("scripts/deploy.py", "python.sql.other", [suppress_case]) is None)
check("rule match but wrong path not suppressed",
      is_suppressed("src/api.py", "python.subprocess.foo", [suppress_case]) is None)
rule_only = SemgrepSuppress(rule_patterns=["broken-rule"], reason="known false positive")
check("empty path prefixes match any path",
      is_suppressed("anywhere/foo.py", "broken-rule", [rule_only]) == "known false positive")

print("\n[8c] semgrep finding coercion")
from reviewer.pipeline.semgrep import coerce_finding
finding = {
    "check_id": "python.lang.security.audit.hardcoded-password",
    "path": "src/api/orders.py",
    "start": {"line": 13},
    "extra": {
        "severity": "ERROR",
        "message": "Hardcoded password detected.",
        "metadata": {"category": "security"},
    },
}
coerced = coerce_finding(finding, fc)
check("semgrep coerced to ReviewComment", coerced is not None)
check("severity mapped to error", coerced.severity == Severity.ERROR, coerced.severity)
check("category mapped to security", coerced.category == Category.SECURITY, coerced.category)
check("line marked diff-added when in added_lines",
      coerced.line_numbers[0].line_number_state == LineState.DIFF_ADDED,
      coerced.line_numbers[0].line_number_state)
off_diff = coerce_finding({**finding, "start": {"line": 5}}, fc)
check("semgrep line outside diff marked as file-context",
      off_diff.line_numbers[0].line_number_state == LineState.FILE_CONTEXT,
      off_diff.line_numbers[0].line_number_state)

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

print("\n[11a] research_codebase composition + recursion guard")
from reviewer.tools.fs_tools import TOOL_SPECS, FileSystemTools
from reviewer.tools.researcher import (
    RESEARCH_TOOL_SPEC, Researcher, build_dispatch, extended_tool_specs,
)
from reviewer.provider.client import AgentEvent, AgentRunResult

_names = [t["function"]["name"] for t in extended_tool_specs()]
check("extended spec exposes research_codebase", "research_codebase" in _names, _names)
check("base tool names preserved in extended spec",
      set(t["function"]["name"] for t in TOOL_SPECS).issubset(_names), _names)

# Stub client that records what the inner agent was given, then returns
# a canned answer. This is what verifies "the inner loop cannot recurse".
class _StubClient:
    def __init__(self, final="Definition at foo.py:10.", turn_limit=False, raise_exc=None):
        self.calls = []
        self._final = final
        self._turn_limit = turn_limit
        self._raise_exc = raise_exc

    def run_agent(self, prompt, *, tool_specs, dispatch, max_turns, label, **_):
        self.calls.append({
            "prompt": prompt, "tool_specs": tool_specs, "max_turns": max_turns,
            "label": label,
        })
        if self._raise_exc is not None:
            raise self._raise_exc
        return AgentRunResult(
            final_output=self._final,
            events=[AgentEvent(kind="tool_call", name="read_file")]
                if not self._turn_limit else [],
            turns_used=max_turns,
            turn_limit_reached=self._turn_limit,
        )

lib = PromptLibrary(res)  # from Section 8
fs = FileSystemTools(Path(__file__).resolve().parents[1])

client_ok = _StubClient()
researcher = Researcher(client_ok, lib, fs)
answer = researcher.research("Where is `total` defined?")
check("researcher returns the model's prose", "foo.py:10" in answer, answer)

inner_specs = client_ok.calls[0]["tool_specs"]
inner_names = [t["function"]["name"] for t in inner_specs]
check("inner loop excludes research_codebase (no recursion)",
      "research_codebase" not in inner_names, inner_names)
check("inner loop retains base fs tools",
      set(t["function"]["name"] for t in TOOL_SPECS) == set(inner_names), inner_names)

# Turn-limit and exception paths must return actionable strings, not raise.
client_stuck = _StubClient(turn_limit=True)
stuck = Researcher(client_stuck, lib, fs).research("Impossible question")
check("turn-limit returns an ERROR string", stuck.startswith("ERROR: research hit"), stuck)

client_boom = _StubClient(raise_exc=RuntimeError("network down"))
boom = Researcher(client_boom, lib, fs).research("anything")
check("exception is caught and returned as data",
      boom.startswith("ERROR: research call failed"), boom)

empty = Researcher(client_ok, lib, fs).research("   ")
check("empty question rejected without a call", empty.startswith("ERROR: research_codebase called with an empty"), empty)

# Composed dispatch: research_codebase routes to the researcher; other names
# fall through to fs_tools. Use a stub-fs to observe the fall-through.
class _StubFs:
    def __init__(self):
        self.saw = None
    def dispatch(self, name, args):
        self.saw = (name, args)
        return "fs-handled"

stub_fs = _StubFs()
class _StubResearcher:
    def research(self, question, focus_paths=None):
        return f"research-handled q={question} focus={focus_paths}"

dispatch = build_dispatch(stub_fs, _StubResearcher())
check("research_codebase routes to researcher",
      "research-handled" in dispatch("research_codebase",
                                     {"question": "Q", "focus_paths": ["a.py"]}))
check("focus_paths passed through",
      "focus=['a.py']" in dispatch("research_codebase",
                                     {"question": "Q", "focus_paths": ["a.py"]}))
check("read_file falls through to fs_tools",
      dispatch("read_file", {"path": "x"}) == "fs-handled" and stub_fs.saw[0] == "read_file")

print("\n[11b] inline review mapping")
from reviewer.pipeline.inline import build_inline_review
from reviewer.models import (
    FileChangeReview as _FCR, PullRequestReview as _PR,
    OverallRating as _OR,
)
_review = _PR(
    cc_id="42",
    file_reviews=[
        _FCR(
            filepath="src/api/orders.py",
            diff="",
            ai_comments=[
                # Straight added-line finding → maps cleanly to RIGHT.
                ReviewComment(
                    line_numbers=[LineNumber(line_number=13, line_number_state=LineState.DIFF_ADDED)],
                    severity=Severity.ERROR, category=Category.LOGIC,
                    message="mutable default", criteria="python-mutable-default",
                    suggestion="use None", rule="python-mutable-default",
                    implementation_complexity=Complexity.LOW, context_needed=False,
                ),
                # Removed-line-only finding → maps to LEFT.
                ReviewComment(
                    line_numbers=[LineNumber(line_number=10, line_number_state=LineState.DIFF_REMOVED)],
                    severity=Severity.WARNING, category=Category.LOGIC,
                    message="removed guard", criteria="general-warning",
                    suggestion="restore the guard", rule="general-warning",
                    implementation_complexity=Complexity.LOW, context_needed=False,
                ),
                # Context-only citation → unmappable, must fall into the body.
                ReviewComment(
                    line_numbers=[LineNumber(line_number=11, line_number_state=LineState.FILE_CONTEXT)],
                    severity=Severity.INFO, category=Category.MAINTAINABILITY,
                    message="docstring missing", criteria="general-info",
                    suggestion="add one", rule="general-info",
                    implementation_complexity=Complexity.LOW, context_needed=False,
                ),
            ],
            overall_rating=_OR.NEEDS_IMPROVEMENT,
        ),
    ],
    overall_rating=_OR.NEEDS_IMPROVEMENT,
    summary="Adds discount handling.",
    total_files=1,
    skipped_files=0,
)
inline = build_inline_review(_review, commit="abc123")
check("body carries the fingerprint", inline.body.startswith("<!-- pr-reviewer"))
check("two inline comments generated", len(inline.comments) == 2, len(inline.comments))
right = [c for c in inline.comments if c.side == "RIGHT"]
left = [c for c in inline.comments if c.side == "LEFT"]
check("added-line comment anchors on RIGHT", right and right[0].line == 13, right)
check("removed-line comment anchors on LEFT", left and left[0].line == 10, left)
check("unmappable comment folds into body",
      len(inline.unmapped) == 1 and "docstring missing" in inline.body, inline.unmapped)
check("summary refers to inline count",
      f"See the {len(inline.comments)} inline comment" in inline.body, inline.body[:200])

print("\n[11c] scan settings + windows")
import json as _json
from datetime import datetime
from reviewer.settings import load_settings, WindowConfig
_tmp = Path("/tmp/pr-reviewer-smoke-settings.json")
_tmp.write_text(_json.dumps({
    "repositories": {
        "acme/api": {"target-branches": ["main", "release/*"], "timerange": 604800}
    },
    "operation": {
        "timezone": "UTC",
        "hours": {
            "active": {"start": "06:00", "end": "21:00", "max_files": 100},
            "inactive": {"start": "21:00", "end": "06:00", "max_files": 500},
        }
    }
}))
_settings = load_settings(_tmp)
check("repository parsed", len(_settings.repositories) == 1 and _settings.repositories[0].url == "acme/api")
check("target branches parsed", _settings.repositories[0].target_branches == ["main", "release/*"])
check("timerange parsed", _settings.repositories[0].timerange == 604800)
active = _settings.operation.window_for(datetime(2026, 1, 1, 10, 0))
check("noon lands in active window", active is not None and active.max_files == 100, active)
inactive = _settings.operation.window_for(datetime(2026, 1, 1, 3, 0))
check("3am lands in wrap-around inactive window",
      inactive is not None and inactive.max_files == 500, inactive)
midnight = _settings.operation.window_for(datetime(2026, 1, 1, 23, 30))
check("late night still inside inactive window",
      midnight is not None and midnight.max_files == 500, midnight)

# Empty/partial config: the loader must not pretend a missing window is always-on.
_tmp.write_text(_json.dumps({"repositories": {}, "operation": {}}))
partial = load_settings(_tmp)
check("empty windows return None", partial.operation.window_for(datetime(2026, 1, 1, 10, 0)) is None)
_tmp.unlink()

print("\n[11d] trigger word detection")
from reviewer.sources.github import trigger_marker
_comments = [
    {"body": "Looks great, LGTM", "created_at": "2026-01-01T00:00:00Z"},
    {"body": "please re-run when ready — !review", "created_at": "2026-01-02T00:00:00Z"},
]
check("bare !review recognised", trigger_marker(_comments) == "!review")
_comments.append({"body": "!do-not-review — this is a WIP", "created_at": "2026-01-01T12:00:00Z"})
check("!do-not-review wins over !review regardless of order",
      trigger_marker(_comments) == "!do-not-review")
check("older !review ignored when since_iso is in the future",
      trigger_marker(_comments[:2], since_iso="2027-01-01T00:00:00Z") is None)

# Trigger routing inside the scan decision. The revision rules themselves are
# exercised in [11e]; these cases check only that a trigger reaches them.
from reviewer.pipeline.scan import _should_review
from reviewer.constants import report_marker as _marker

_PR = {"number": 7, "headRefOid": "feed1234", "updatedAt": "2026-01-05T00:00:00Z"}

review_flag, reason = _should_review(_PR, _comments, reports=[])
check("do-not-review kills the review decision", not review_flag and "opted out" in reason, reason)

_reviewed = [{"body": _marker("feed1234"), "created_at": "2026-01-01T00:00:00Z"}]
review_flag, reason = _should_review(
    _PR, [{"body": "please !review", "created_at": "2026-01-05T00:00:00Z"}], _reviewed
)
check("!review re-reviews a revision that already has a report",
      review_flag and "!review" in reason, reason)

review_flag, reason = _should_review(
    _PR, [{"body": "just a note", "created_at": "2026-01-11T00:00:00Z"}], _reviewed
)
check("an ordinary comment does not re-trigger a reviewed revision",
      not review_flag and "already reviewed" in reason, reason)

print("\n[11e] scan: revision-keyed dedup, trigger expiry, checkout scoping")
from reviewer.pipeline.scan import _should_review
from reviewer.constants import report_marker
from reviewer.sources.github import parse_reviewed_sha

def _rep(sha, at="2026-08-20T10:00:00Z"):
    return {"body": report_marker(sha) + "\n# Code review report", "created_at": at}
def _cm(body, at="2026-08-19T12:00:00Z"):
    return {"body": body, "created_at": at}
def _pr(sha, updated="2026-08-20T09:00:00Z"):
    return {"number": 7, "headRefOid": sha, "updatedAt": updated}

check("marker round-trips the revision",
      parse_reviewed_sha(report_marker("abc1234def")) == "abc1234def")
check("a report predating the sha marker is not a dedup answer",
      parse_reviewed_sha("<!-- pr-reviewer:code_review_report --> old") is None)

# The race the timestamp rule lost: a push landing while a review runs.
ok, why = _should_review(_pr("bbbb2222"), [], [_rep("aaaa1111", "2026-08-20T11:00:00Z")])
check("a newer report for an older commit does not suppress the new one", ok, why)

ok, why = _should_review(_pr("aaaa1111"), [], [_rep("aaaa1111")])
check("the same revision is never reviewed twice", ok is False and "already reviewed" in why, why)
ok, why = _should_review(_pr("aaaa1111"), [], [])
check("an unreviewed revision is reviewed", ok, why)
ok, why = _should_review(_pr(""), [], [])
check("no revision means no review, rather than a guess", ok is False, why)

# Triggers keep their own, timestamp-shaped semantics: they are point-in-time
# requests, answered by any report that followed them.
ok, why = _should_review(_pr("aaaa1111"), [_cm("please !review", "2026-08-20T12:00:00Z")], [_rep("aaaa1111")])
check("!review overrides an already-reviewed revision", ok and "!review" in why, why)
ok, why = _should_review(_pr("aaaa1111"), [_cm("!review", "2026-08-19T00:00:00Z")], [_rep("aaaa1111")])
check("a !review already answered by a report does not re-fire", ok is False, why)
ok, why = _should_review(_pr("cccc3333"), [_cm("!do-not-review")], [])
check("opt-out beats an unreviewed revision", ok is False and "opted out" in why, why)

# Pinning a review to the right checkout moved to sources/checkout.py and is
# exercised against a real repository in smoke_serve.py.

print("\n[11f] retry outcomes: degrade vs terminal, and jitter")
import tempfile as _tf
from reviewer.exception_handling import (
    BoundedRetryPolicy, DegradedCall, NeverTerminatePolicy, with_jitter, with_retries,
)

def _always(exc):
    def _f(): raise exc
    return _f

# A bounded policy exhausts its budget and hands back the caller's own error.
try:
    with_retries(_always(RuntimeError("Error code: 402 - Insufficient Balance")),
                 label="t", policy=BoundedRetryPolicy())
    check("terminal raises", False, "no error")
except DegradedCall:
    check("terminal raises the original, not DegradedCall", False, "got DegradedCall")
except RuntimeError as e:
    check("terminal raises the original, not DegradedCall", "402" in str(e), str(e)[:50])

# The never-terminate policy degrades on a deterministic failure instead.
try:
    with_retries(_always(RuntimeError("Error code: 402 - Insufficient Balance")),
                 label="t", policy=NeverTerminatePolicy())
    check("degrade raises", False, "no error")
except DegradedCall as d:
    check("degrade raises DegradedCall", True)
    check("degrade names the subject", d.subject == "t", d.subject)
    check("degrade keeps the original", "402" in str(d.original), str(d.original)[:40])
    check("degrade chains the cause", isinstance(d.__cause__, RuntimeError))

# A loop can therefore catch one and not the other.
def _sweep(errors):
    survived = []
    for name, exc in errors:
        try:
            with_retries(_always(exc), label=name, policy=NeverTerminatePolicy())
        except DegradedCall:
            survived.append(name)
    return survived
check("a loop skips degraded subjects and continues",
      _sweep([("a", RuntimeError("402 -")), ("b", RuntimeError("402 -"))]) == ["a", "b"])

# Jitter is proportional and never exceeds the requested wait.
samples = [with_jitter(3600.0, spread=0.5) for _ in range(400)]
check("jitter never exceeds the cap", max(samples) <= 3600.0, max(samples))
check("jitter respects the spread floor", min(samples) >= 1800.0, min(samples))
check("jitter actually spreads a long wait", max(samples) - min(samples) > 1000.0,
      max(samples) - min(samples))
check("jitter scales with the wait", with_jitter(0.0) == 0.0)

print("\n[11f2] subscription usage windows")
import time as _t
from reviewer.exception_handling import (
    BoundedRetryPolicy as _B, NeverTerminatePolicy as _N,
    UsageLimitError, looks_like_usage_limit,
)
check("usage limit classifies as capacity, not fatal",
      classify(UsageLimitError("x")) is ErrorKind.CAPACITY)
check("a rate limit is not a usage window", not looks_like_usage_limit("rate limit exceeded"))
for phrase in ("Claude usage limit reached. Resets at 3pm.", "5-hour limit reached", "weekly limit"):
    check(f"recognised: {phrase[:28]!r}", looks_like_usage_limit(phrase))

# A window that reopens in two hours must not be retried on a 30-second curve.
for name, pol in (("bounded", _B()), ("never-terminate", _N())):
    o = pol.decide(UsageLimitError("limit", resets_at=_t.time() + 7200), subject=f"w-{name}")
    check(f"{name}: waits for the window, not the curve", 7200 <= o.wait_s <= 7260, o.wait_s)
    o2 = pol.decide(UsageLimitError("limit"), subject=f"u-{name}")
    check(f"{name}: unknown reset falls back to the curve", o2.wait_s < 120, o2.wait_s)
check("seconds_remaining is never negative",
      UsageLimitError("x", resets_at=_t.time() - 500).seconds_remaining() == 0.0)
check("no reset means no answer", UsageLimitError("x").seconds_remaining() is None)

print("\n[11g] scan lock")
from reviewer.locking import LockHeld, exclusive
lock = Path(_tf.mkdtemp()) / "scan.lock"
with exclusive(lock, label="first"):
    check("lock file exists while held", lock.exists())
    try:
        with exclusive(lock, label="second"):
            check("second holder rejected", False, "it got in")
    except LockHeld:
        check("second holder rejected", True)
check("lock released on exit", not lock.exists())

try:
    with exclusive(lock, label="boom"):
        raise ValueError("body failed")
except ValueError:
    pass
check("lock released even when the body raises", not lock.exists())

lock.write_text('{"pid": 999999, "taken_at": 0}')
with exclusive(lock, ttl_s=1.0, label="takeover"):
    check("stale lock is taken over", True)
check("taken-over lock released", not lock.exists())

lock.write_text('{"pid": 999999, "taken_at": %f}' % (__import__("time").time()))
try:
    with exclusive(lock, ttl_s=3600.0, label="fresh"):
        check("fresh foreign lock honoured", False, "it got in")
except LockHeld:
    check("fresh foreign lock honoured", True)

print("\n[11h] trigger boundaries, timestamps, research budget")
from reviewer.sources.github import trigger_marker as _tm
from reviewer.timestamps import is_after, newest, parse_iso
from reviewer.tools.researcher import DEFAULT_RESEARCH_CALLS, build_dispatch

def _cm(body, at="2026-08-19T12:00:00Z"): return {"body": body, "created_at": at}

check("!reviewer does not trigger a review", _tm([_cm("ask !reviewer to look")]) is None)
check("!review inside prose still triggers", _tm([_cm("please !review this")]) == "!review")
check("!reviews (plural) does not trigger", _tm([_cm("see !reviews")]) is None)
check("trigger match is case-insensitive", _tm([_cm("!DO-NOT-REVIEW")]) == "!do-not-review")
check("opt-out beats a trigger in the same sweep",
      _tm([_cm("!review"), _cm("!do-not-review")]) == "!do-not-review")

# Z and +00:00 are the same instant; string comparison ranks Z after +.
check("Z and +00:00 compare as equal instants",
      is_after("2026-01-01T00:00:00Z", "2026-01-01T00:00:00+00:00") is False)
check("string comparison would have got that wrong",
      "2026-01-01T00:00:00Z" > "2026-01-01T00:00:00+00:00")
check("a real ordering still holds across spellings",
      is_after("2026-01-01T00:00:01Z", "2026-01-01T00:00:00+00:00"))
check("no reference means no bound", is_after("2026-01-01T00:00:00Z", None))
check("unreadable candidate is not after anything", is_after("not-a-date", None) is False)
check("naive timestamps are read as UTC",
      parse_iso("2026-01-01T00:00:00").tzinfo is not None)
check("newest picks the latest across spellings",
      newest(["2026-01-01T00:00:00+00:00", "2026-06-01T00:00:00Z", "junk"])
      == "2026-06-01T00:00:00Z")
check("newest of nothing is empty", newest(["junk", ""]) == "")

# The research budget belongs to one dispatch, so a new review gets a new one.
class _StubResearcher:
    def __init__(self): self.calls = 0
    def research(self, question, focus_paths=None):
        self.calls += 1
        return f"answer {self.calls}"

class _StubFs:
    def dispatch(self, name, args): return f"fs:{name}"

stub = _StubResearcher()
disp = build_dispatch(_StubFs(), stub, max_calls=2)
outs = [disp("research_codebase", {"question": f"q{i}"}) for i in range(4)]
check("budget allows exactly max_calls researches", stub.calls == 2, stub.calls)
check("over-budget calls are refused, not raised", outs[2].startswith("ERROR:"), outs[2][:40])
check("refusal tells the model to conclude", "Conclude" in outs[2])
check("fs tools are unaffected by the research budget",
      disp("read_file", {"path": "x"}) == "fs:read_file")
fresh = build_dispatch(_StubFs(), stub, max_calls=2)
fresh("research_codebase", {"question": "q"})
check("a new dispatch gets a fresh budget", stub.calls == 3, stub.calls)
check("default budget is documented", DEFAULT_RESEARCH_CALLS == 5)

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
