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
p1 = PrPart(pr_id="r#1", matcher_version="judge-v1",
            ground_truth=[gt("1", False, "p1"), gt("2", True, "p2"), gt("3", True, "p1")],
            findings=[sf("error", "1"), sf("error", "1"), sf("warning", "2"), sf("info", None)])
# PR2: deliberately clean; every finding is a false positive.
p2 = PrPart(pr_id="r#2", matcher_version="judge-v1", ground_truth=[],
            findings=[sf("warning", None)])
overall, per_sev, by_diff, by_value = score_parts([p1, p2])

check("true positives counted per finding", overall.true_positives == 3, overall.true_positives)
check("false positives counted", overall.false_positives == 2, overall.false_positives)
check("known bugs counted once each", overall.ground_truth == 3, overall.ground_truth)
check("duplicate hit does not double-credit recall", overall.recalled == 2, overall.recalled)
check("precision counts findings", abs(overall.precision - 0.6) < 1e-9, overall.precision)
check("recall counts bugs, not findings", abs(overall.recall - 2/3) < 1e-9, overall.recall)
check("f1", abs(overall.f1 - (2*0.6*(2/3))/(0.6+2/3)) < 1e-9, overall.f1)

# The regression this replaced: a run that stops duplicating a hit keeps the
# same recall instead of appearing to lose ground.
dedup = PrPart(pr_id="r#1b", matcher_version="judge-v1",
               ground_truth=[gt("1", False, "p1"), gt("2", True, "p2"), gt("3", True, "p1")],
               findings=[sf("error", "1"), sf("warning", "2"), sf("info", None)])
o_dedup, *_ = score_parts([dedup, p2])
check("dropping a duplicate finding does not change recall",
      abs(o_dedup.recall - overall.recall) < 1e-9, (o_dedup.recall, overall.recall))
# Precision intentionally still counts findings: two comments about one real
# bug are both real to the developer reading them, so removing one lowers the
# numerator. Only recall is bug-scoped.
check("dropping a duplicate true positive lowers precision",
      o_dedup.precision < overall.precision, (o_dedup.precision, overall.precision))
check("per-severity error precision", per_sev["error"].precision == 1.0, per_sev["error"].precision)
check("per-severity info precision", per_sev["info"].precision == 0.0, per_sev["info"].precision)

print("\n[buckets]")
check("diff-only recall", by_diff["diff-only"].value == 1.0, (by_diff["diff-only"].found, by_diff["diff-only"].total))
check("cross-file recall", by_diff["cross-file"].value == 0.5, (by_diff["cross-file"].found, by_diff["cross-file"].total))
check("p1 recall", by_value["p1"].value == 0.5, (by_value["p1"].found, by_value["p1"].total))
check("p2 recall", by_value["p2"].value == 1.0, (by_value["p2"].found, by_value["p2"].total))

print("\n[guards]")
mixed = [p1, PrPart(pr_id="r#3", matcher_version="judge-v2")]
try:
    score_parts(mixed)
    check("mixed matcher versions rejected", False, "no error raised")
except ValueError:
    check("mixed matcher versions rejected", True)

failed = PrPart(pr_id="r#4", matcher_version="judge-v1", failed=True, error="boom")
o2, *_ = score_parts([p1, failed])
check("failed parts excluded from scoring", o2.true_positives == 3, o2.true_positives)
check("failed parts contribute no ground truth", o2.ground_truth == 3, o2.ground_truth)

print("\n[report]")
report = format_report(overall, per_sev, by_diff, by_value, [p1, p2, failed])
check("report has precision row", "| precision |" in report)
check("report separates bugs from findings", "known bugs found" in report and "findings that matched" in report)
check("report lists failures", "`r#4`: boom" in report)
check("report shows difficulty buckets", "Recall by difficulty" in report)
check("report counts clean PRs", "deliberately clean" in report)

print("\n[3-class judge collapse]")
from reviewer.benchmark.model import JudgeVerdict, RawFinding
from reviewer.benchmark.gold import GoldVote, _settle

def _vote(idx, verdict, gt_id=None):
    return GoldVote(pass_index=idx, verdict=verdict, matched_gt_id=gt_id,
                    reasoning="r")

raw = RawFinding(file="a.py", lines=[1], severity="error", category="logic",
                 message="m", suggestion="s")

# Unanimous MATCH with the same gt id → settled.
settled = _settle(finding_id="f1", pr_id="pr1", raw=raw, votes=[
    _vote(1, JudgeVerdict.MATCH, "gt-1"),
    _vote(2, JudgeVerdict.MATCH, "gt-1"),
    _vote(3, JudgeVerdict.MATCH, "gt-1"),
])
check("unanimous MATCH settles", settled.settled_verdict == JudgeVerdict.MATCH.value)
check("settled gt id carried", settled.settled_gt_id == "gt-1")

# Unanimous MATCH but different gt ids → TODO. Same verdict is not the same
# answer if the passes disagree on which bug it is.
split_ids = _settle(finding_id="f2", pr_id="pr1", raw=raw, votes=[
    _vote(1, JudgeVerdict.MATCH, "gt-1"),
    _vote(2, JudgeVerdict.MATCH, "gt-2"),
])
check("MATCH with split gt ids stays TODO", split_ids.settled_verdict == "TODO",
      split_ids.settled_verdict)

# Verdict split → TODO.
mixed_votes = _settle(finding_id="f3", pr_id="pr1", raw=raw, votes=[
    _vote(1, JudgeVerdict.MATCH, "gt-1"),
    _vote(2, JudgeVerdict.PARTIAL, "gt-1"),
    _vote(3, JudgeVerdict.NO_MATCH),
])
check("verdict disagreement stays TODO", mixed_votes.settled_verdict == "TODO")

# No votes at all → TODO with the default.
no_votes = _settle(finding_id="f4", pr_id="pr1", raw=raw, votes=[])
check("no votes falls back to TODO", no_votes.settled_verdict == "TODO")

# Unanimous PARTIAL should settle even if the ids disagree — PARTIAL does not
# claim identity, so we do not require id agreement.
partial = _settle(finding_id="f5", pr_id="pr1", raw=raw, votes=[
    _vote(1, JudgeVerdict.PARTIAL, "gt-1"),
    _vote(2, JudgeVerdict.PARTIAL, "gt-2"),
])
check("unanimous PARTIAL settles", partial.settled_verdict == JudgeVerdict.PARTIAL.value)

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
