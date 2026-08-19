"""Hand-written matcher calibration cases.

Grow this to 10-20 alongside the gold corpus. Every case is a judgement a
human already made, so a disagreement means the matcher moved, not the truth.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from reviewer.benchmark.calibration import CalibrationCase
from reviewer.benchmark.model import GroundTruthIssue, RawFinding


def _f(message, file="BridgeManager.java", severity="error", suggestion=""):
    return RawFinding(file=file, lines=[1], severity=severity, category="logic",
                      message=message, suggestion=suggestion)


_INDEX_BUG = GroundTruthIssue(
    id="13947-1", file="BridgeManager.java",
    description="changes.get(0) hardcoded inside for(j) loop — should be changes.get(j). "
                "All ports after the first receive the wrong enforcement state.",
    min_severity="error", value="p1", requires_exploration=True,
)
_BOXED_EQUALITY = GroundTruthIssue(
    id="mgr-2", file="Manager.java",
    description="Long == reference equality instead of .equals() at Manager.java:3712",
    min_severity="error", value="p1",
)
_THREAD_LEAK = GroundTruthIssue(
    id="pool-1", file="Loader.java",
    description="Executors.newSingleThreadExecutor() at lines 88 and 171 are never shut down",
    min_severity="error", value="p2",
)
_SWALLOWED = GroundTruthIssue(
    id="svc-1", file="LogReceiverService.java",
    description="Persistence call wrapped in catch (Exception ignore) {} — failure is swallowed "
                "and the client still receives a success response.",
    min_severity="error", value="p2",
)

_INDEX_DIFF = ("-  retval.add(new RoleChange(changes.get(j).roleName,\n"
               "+  retval.add(new RoleChange(changes.get(0).roleName,")

CALIBRATION_CASES = [
    CalibrationCase(
        label="exact_hardcoded_index",
        finding=_f("Hardcoded index get(0) used inside port iteration loop instead of the loop variable"),
        ground_truth=[_INDEX_BUG], diff_snippet=_INDEX_DIFF, expected_gt_id="13947-1",
    ),
    CalibrationCase(
        label="alt_wording_same_defect",
        finding=_f("Loop variable j is ignored; every iteration reads element zero, so only the "
                   "first port gets the right state"),
        ground_truth=[_INDEX_BUG], diff_snippet=_INDEX_DIFF, expected_gt_id="13947-1",
    ),
    CalibrationCase(
        label="same_file_different_defect",
        finding=_f("Missing null check on status.getResult()", file="Manager.java"),
        ground_truth=[_BOXED_EQUALITY], diff_snippet="+  if (a == b) {", expected_gt_id=None,
    ),
    CalibrationCase(
        label="partial_scope_still_matches_in_binary",
        finding=_f("Executor thread pool created but never shut down — thread leak on each reload cycle",
                   file="Loader.java"),
        ground_truth=[_THREAD_LEAK],
        diff_snippet="+  ExecutorService ex = Executors.newSingleThreadExecutor();",
        expected_gt_id="pool-1",
    ),
    CalibrationCase(
        label="plausible_but_different_defect",
        finding=_f("Exception handling is too broad — catching Exception instead of specific types",
                   file="LogReceiverService.java"),
        ground_truth=[_SWALLOWED],
        diff_snippet="+  } catch (Exception ignore) {}",
        expected_gt_id=None,
    ),
    CalibrationCase(
        label="real_finding_not_in_gold_set",
        finding=_f("Unused import java.util.Date", severity="info"),
        ground_truth=[_INDEX_BUG], diff_snippet=_INDEX_DIFF, expected_gt_id=None,
    ),
    CalibrationCase(
        label="picks_best_of_several",
        finding=_f("Executor is never shut down, leaking one thread per reload", file="Loader.java"),
        ground_truth=[_INDEX_BUG, _THREAD_LEAK, _BOXED_EQUALITY],
        diff_snippet="+  ExecutorService ex = Executors.newSingleThreadExecutor();",
        expected_gt_id="pool-1",
    ),
    CalibrationCase(
        label="empty_gold_set_never_matches",
        finding=_f("Hardcoded index get(0) inside the loop"),
        ground_truth=[], diff_snippet=_INDEX_DIFF, expected_gt_id=None,
    ),
]
