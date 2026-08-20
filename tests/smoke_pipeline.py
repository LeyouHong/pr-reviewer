"""End-to-end wiring check with a stubbed model. No network, no API key."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from reviewer.config import Config
from reviewer.diffing.parser import parse_unified_diff
from reviewer.models import (
    Category, CodeChangeInfo, Complexity, LineNumber, LineState, LLMFileChangeReview,
    Maintainability, Metrics, OverallRating, ReviewComment, Severity, TestCoverage,
)
from reviewer.pipeline.orchestrator import ReviewPipeline
from reviewer.provider.client import AgentEvent, AgentRunResult

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

def comment(line, sev, msg, sugg, cat=Category.LOGIC):
    return ReviewComment(
        line_numbers=[LineNumber(line_number=line, line_number_state=LineState.DIFF_ADDED)],
        severity=sev, category=cat, message=msg, criteria="python-mutable-default",
        suggestion=sugg, rule="python-mutable-default",
        implementation_complexity=Complexity.LOW, context_needed=False,
    )

CALLS = {"structured": 0, "text": [], "agent": 0}

class StubClient:
    def complete_structured(self, prompt, schema, **kw):
        CALLS["structured"] += 1
        assert "def total(items, discounts=[])" in prompt, "diff missing from reviewer prompt"
        assert "python-mutable-default" in prompt, "language rules missing from reviewer prompt"
        return LLMFileChangeReview(
            overall_rating=OverallRating.NEEDS_IMPROVEMENT,
            summary="Adds discount handling; found a shared-default defect and a fix narration.",
            comments=[
                comment(13, Severity.ERROR,
                        "Mutable default argument `discounts=[]` is shared across every call.",
                        "Default to None and create the list inside the function."),
                comment(15, Severity.WARNING,
                        "The previous code lacked discount support; the fix correctly adds it.",
                        "No change needed."),
                comment(9999, Severity.ERROR,
                        "Fabricated citation outside the diff.", "Nothing."),
            ],
            metrics=Metrics(complexity=Complexity.LOW, test_coverage=TestCoverage.NONE,
                            maintainability=Maintainability.MEDIUM),
        )

    def complete_text(self, prompt, **kw):
        label = kw.get("label", "")
        CALLS["text"].append(label.split(":")[0])
        if label.startswith("qualify"):
            assert "Lines added by this diff" in prompt, "scope section missing from qualify prompt"
            return "The defect is self-evident but touches callers.\nvalidate"
        if label.startswith("patch_summary"):
            assert "the fix correctly adds it" in prompt, "removed issues not shown to patch_summary"
            assert "Mutable default argument" in prompt, "retained issues not shown to patch_summary"
            return "Adds discount handling with a shared mutable default in `total`."
        return "Found 1 error in src/api/orders.py. Please see details."

    def run_agent(self, prompt, **kw):
        CALLS["agent"] += 1
        assert "Scope Fence" in prompt, "scope fence missing from validator prompt"
        return AgentRunResult(
            final_output="Read the file and confirmed the default is shared.\nVerdict: TRUE_POSITIVE",
            events=[AgentEvent(kind="tool_call", name="read_file", payload="{}")],
            turns_used=2,
        )

config = Config(api_key="stub", repo_path=Path.cwd())
pipeline = ReviewPipeline.__new__(ReviewPipeline)
from reviewer.policy import PolicyRouter
from reviewer.prompt import PromptLibrary
from reviewer.tools.fs_tools import FileSystemTools
from reviewer.pipeline.qualify import Qualifier
from reviewer.pipeline.render import Summarizer
from reviewer.pipeline.review import FileReviewer
from reviewer.pipeline.validate import Validator

stub = StubClient()
pipeline.config = config
pipeline.library = PromptLibrary(config.resources_dir)
pipeline.router = PolicyRouter(config.resources_dir)
pipeline.client = stub
pipeline.tools = FileSystemTools(config.repo_path)
pipeline.reviewer = FileReviewer(stub, pipeline.library, pipeline.router, config)
pipeline.qualifier = Qualifier(stub, pipeline.library)
pipeline.validator = Validator(stub, pipeline.library, pipeline.tools)
pipeline.summarizer = Summarizer(stub, pipeline.library)

info = CodeChangeInfo(
    repository="acme/api", cc_id="42", cc_title="Add discount support",
    cc_description="Applies discounts to order totals.",
    source_branch="feat/discounts", target_branch="main", head_sha="c0ffee1234abcd",
    changes=parse_unified_diff(DIFF),
)

result = pipeline.run(info)
markdown = pipeline.render(result, commit="abc1234")

FAILURES = []
def check(name, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + name + (f"  {detail}" if not cond else ""))
    if not cond: FAILURES.append(name)

print("\n[pipeline]")
check("reviewer called once", CALLS["structured"] == 1, CALLS["structured"])
check("exactly one comment survived", result.total_comments == 1, result.total_comments)
kept = result.file_reviews[0].ai_comments[0]
check("survivor is the real defect", "Mutable default" in kept.message, kept.message)
check("fix narration removed by gate", all("correctly adds" not in c.message for c in result.file_reviews[0].ai_comments))
check("fabricated citation never reached the gate", CALLS["text"].count("qualify") == 1, CALLS["text"])
check("out-of-scope recorded", len(result.file_reviews[0].out_of_scope_comments) == 1)
check("validator ran once", CALLS["agent"] == 1, CALLS["agent"])
check("patch_summary ran", "patch_summary" in CALLS["text"], CALLS["text"])
check("overall summary ran", "summarize" in CALLS["text"], CALLS["text"])
check("stale summary was rewritten", "fix narration" not in result.file_reviews[0].summary, result.file_reviews[0].summary)
check("error count", result.error_count == 1, result.error_count)
check("rating downgraded", result.overall_rating is OverallRating.NEEDS_IMPROVEMENT, result.overall_rating)

print("\n[markdown]")
check("marker present", "<!-- pr-reviewer:code_review_report" in markdown)
check("marker records the reviewed revision",
      "sha=c0ffee1234abcd" in markdown, markdown.splitlines()[0])
check("reviewed commit shown in diagnostics", "Reviewed commit: `c0ffee1234abcd`" in markdown)
from reviewer.sources.github import parse_reviewed_sha
check("the posted report answers a dedup query",
      parse_reviewed_sha(markdown) == "c0ffee1234abcd")
check("file heading present", "## `src/api/orders.py`" in markdown)
check("counts precede content", markdown.index("- Errors: 1") < markdown.index("Mutable default"))
check("severity grouping", "### Errors" in markdown)
check("diagnostics collapsed", "<details><summary>Diagnostics</summary>" in markdown)
check("skipped count reported", "- Skipped: 0" in markdown)
check("finding rendered", "Mutable default argument" in markdown)
check("suggestion rendered", "Default to None" in markdown)
check("narration absent from report", "correctly adds" not in markdown)
check("build stamp present", "abc1234" in markdown)

print("\n--- rendered report ---")
print(markdown)

if FAILURES:
    print(f"{len(FAILURES)} FAILED: {FAILURES}")
    sys.exit(1)
print("pipeline wiring OK")
