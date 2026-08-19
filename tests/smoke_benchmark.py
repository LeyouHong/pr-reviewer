"""Offline check of benchmark scoring math and corpus round-trip."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from reviewer.benchmark.model import Corpus, CorpusPr, GroundTruthIssue, PrPart, ScoredFinding
from reviewer.benchmark.scoring import format_report, score_parts

F = []
def check(n, c, d=""):
    print(("  PASS  " if c else "  FAIL  ") + n + (f"  {d}" if not c else ""))
    if not c: F.append(n)

def sf(sev, matched):
    return ScoredFinding(file="a.py", lines=[1], severity=sev, category="logic",
                         message="m", suggestion="s", matched_gt_id=matched)

print("\n[scoring]")
def gt(i, explore, value):
    return GroundTruthIssue(id=i, file="a.py", description="d",
                            requires_exploration=explore, value=value)

# PR1: 3 known bugs. Reviewer finds 2 of them (one twice) plus 1 false positive.
p1 = PrPart(pr_id="r#1", matcher_version="binary-v1",
            ground_truth=[gt("1", False, "p1"), gt("2", True, "p2"), gt("3", True, "p1")],
            findings=[sf("error", "1"), sf("error", "1"), sf("warning", "2"), sf("info", None)])
# PR2: deliberately clean; every finding is a false positive.
p2 = PrPart(pr_id="r#2", matcher_version="binary-v1", ground_truth=[],
            findings=[sf("warning", None)])
overall, per_sev, by_diff, by_value = score_parts([p1, p2])

check("true positives counted per finding", overall.true_positives == 3, overall.true_positives)
check("false positives counted", overall.false_positives == 2, overall.false_positives)
check("duplicate hit does not double-credit recall", overall.false_negatives == 1, overall.false_negatives)
check("precision", abs(overall.precision - 0.6) < 1e-9, overall.precision)
check("recall", abs(overall.recall - 0.75) < 1e-9, overall.recall)
check("f1", abs(overall.f1 - (2*0.6*0.75)/(0.6+0.75)) < 1e-9, overall.f1)
check("per-severity error precision", per_sev["error"].precision == 1.0, per_sev["error"].precision)
check("per-severity info precision", per_sev["info"].precision == 0.0, per_sev["info"].precision)

print("\n[buckets]")
check("diff-only recall", by_diff["diff-only"].value == 1.0, (by_diff["diff-only"].found, by_diff["diff-only"].total))
check("cross-file recall", by_diff["cross-file"].value == 0.5, (by_diff["cross-file"].found, by_diff["cross-file"].total))
check("p1 recall", by_value["p1"].value == 0.5, (by_value["p1"].found, by_value["p1"].total))
check("p2 recall", by_value["p2"].value == 1.0, (by_value["p2"].found, by_value["p2"].total))

print("\n[guards]")
mixed = [p1, PrPart(pr_id="r#3", matcher_version="binary-v2")]
try:
    score_parts(mixed)
    check("mixed matcher versions rejected", False, "no error raised")
except ValueError:
    check("mixed matcher versions rejected", True)

failed = PrPart(pr_id="r#4", matcher_version="binary-v1", failed=True, error="boom")
o2, *_ = score_parts([p1, failed])
check("failed parts excluded from scoring", o2.true_positives == 3, o2.true_positives)

print("\n[report]")
report = format_report(overall, per_sev, by_diff, by_value, [p1, p2, failed])
check("report has precision row", "| precision |" in report)
check("report lists failures", "`r#4`: boom" in report)
check("report shows difficulty buckets", "Recall by difficulty" in report)
check("report counts clean PRs", "deliberately clean" in report)

print("\n[corpus round-trip]")
c = Corpus(prs=[CorpusPr(id="acme/api#7", repo="acme/api", number=7, diff="x", pin_commit="deadbeef",
                         ground_truth=[GroundTruthIssue(id="7-1", file="a.py", lines=[3], description="d",
                                                        min_severity="error", value="p1",
                                                        requires_exploration=True,
                                                        note="amended: re-anchored at pin")],
                         labelled=True)])
back = Corpus.model_validate_json(c.model_dump_json())
check("corpus serialises", back.prs[0].ground_truth[0].id == "7-1")
check("pin commit retained", back.prs[0].pin_commit == "deadbeef")
check("value tier retained", back.prs[0].ground_truth[0].value == "p1")
check("amendment note retained", "re-anchored" in back.prs[0].ground_truth[0].note)

print()
if F:
    print(f"{len(F)} FAILED: {F}"); sys.exit(1)
print("benchmark checks passed")
