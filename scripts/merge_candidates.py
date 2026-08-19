#!/usr/bin/env python3
"""Fold accepted candidates from candidates.json into corpus.json.

Workflow: delete the candidates you reject, edit the ones you keep, then run
this. PRs with no entry in candidates.json (the clean-PR controls) are marked
labelled with an empty ground_truth, which is what measures whether the
reviewer invents findings.

Re-running is safe: a candidate id already present in the corpus is replaced,
so you can iterate on wording without duplicating entries.
"""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CORPUS = ROOT / "corpus.json"
CANDIDATES = ROOT / "candidates.json"

FIELDS = {"id", "file", "lines", "description", "min_severity", "value",
          "requires_exploration", "note"}


def main() -> int:
    corpus = json.loads(CORPUS.read_text(encoding="utf-8"))
    accepted = {
        k: v for k, v in json.loads(CANDIDATES.read_text(encoding="utf-8")).items()
        if not k.startswith("_")
    }

    known = {pr["id"] for pr in corpus["prs"]}
    unknown = set(accepted) - known
    if unknown:
        print(f"error: candidates reference PRs not in the corpus: {sorted(unknown)}")
        return 1

    for pr in corpus["prs"]:
        issues = accepted.get(pr["id"], [])
        for issue in issues:
            extra = set(issue) - FIELDS
            if extra:
                print(f"error: {issue.get('id')} has unknown field(s): {sorted(extra)}")
                return 1
            if not issue.get("description", "").strip():
                print(f"error: {issue.get('id')} has an empty description")
                return 1

        incoming = {i["id"] for i in issues}
        kept = [g for g in pr.get("ground_truth", []) if g["id"] not in incoming]
        pr["ground_truth"] = kept + issues
        pr["labelled"] = True

        marker = "clean" if not pr["ground_truth"] else f"{len(pr['ground_truth'])} issue(s)"
        print(f"  {pr['id']:<26} {marker}")

    CORPUS.write_text(json.dumps(corpus, indent=2, ensure_ascii=False) + "\n",
                      encoding="utf-8")
    total = sum(len(pr["ground_truth"]) for pr in corpus["prs"])
    clean = sum(1 for pr in corpus["prs"] if not pr["ground_truth"])
    print(f"\n{len(corpus['prs'])} PR(s) labelled, {total} ground-truth issue(s), "
          f"{clean} clean control(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
