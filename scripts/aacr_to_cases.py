#!/usr/bin/env python3
"""Convert AACR-Bench into validation cases this pipeline can be scored on.

AACR-Bench measures a different thing from corpus.json, and the difference is
the point. A defect corpus asks "given a diff, what did the reviewer find?" —
it measures recall. AACR-Bench hands you a review comment already written and
asks whether it is correct, which is what the qualification gate and the deep
validator do. Those two stages have never been measured against anything but
labels this project wrote itself.

It also supplies the thing that is hardest to produce by hand: 640 comments an
expert judged wrong, 1,597 of them machine-written — including from the same
model family this pipeline runs on. That is the false-positive distribution the
validator actually faces, not a synthetic one.

Source: https://huggingface.co/datasets/Alibaba-Aone/aacr-bench (Apache-2.0)

What the conversion has to reconstruct:

* The diff, from `gh pr diff`. The recorded commit pair looks like the obvious
  input, but comparing them returns everything that diverged between the two
  branches — 300 files and 366 commits on a FreeCAD pull request, capped by the
  API, with the reviewed file usually not among them. The pull request's own
  diff contains it every time. Nothing is cloned either way.
* A ReviewComment. `note` is the claim, `path`/`from_line`/`to_line` place it,
  and `category` maps onto ours. Severity is not recorded, so it is inferred
  from the category: the validator's cheap gate treats info differently from
  error, and calling everything a warning would exercise one path only.

Usage:
    scripts/aacr_to_cases.py --out cases.json --sample 300
    scripts/aacr_to_cases.py --out diff-only.json --context "Diff Level"
    scripts/aacr_to_cases.py --out ts.json --language TypeScript --sample 100
"""

from __future__ import annotations

import argparse
import json
import random
import re
import subprocess
import sys
import urllib.request
from collections import Counter, defaultdict
from pathlib import Path

DATASET = (
    "https://huggingface.co/datasets/Alibaba-Aone/aacr-bench/"
    "resolve/main/dataset.json"
)

# AACR-Bench's four categories onto ours. Ours has six; the two with no source
# category (style, testing) simply never appear, which is honest — inventing a
# mapping would put findings in buckets the dataset never labelled.
CATEGORY = {
    "Code Defect": "logic",
    "Maintainability and Readability": "maintainability",
    "Performance": "performance",
    "Security Vulnerability": "security",
}

# Severity is not in the dataset, and the gate routes on it: info goes to the
# actionability judge, error and warning to the validator. Deriving it from the
# category keeps both paths exercised in roughly the proportion a real review
# produces, rather than sending everything down one.
SEVERITY = {
    "security": "error",
    "logic": "warning",
    "performance": "info",
    "maintainability": "info",
}

_PR_URL = re.compile(r"https://github\.com/([^/]+/[^/]+)/pull/(\d+)")
_DIFF_GIT = re.compile(r"^diff --git a/.+? b/(.+?)$")


def load_dataset(cache: Path) -> list[dict]:
    if cache.exists():
        return json.loads(cache.read_text(encoding="utf-8"))
    print(f"downloading {DATASET}", file=sys.stderr)
    with urllib.request.urlopen(DATASET, timeout=120) as response:
        raw = response.read().decode("utf-8")
    cache.write_text(raw, encoding="utf-8")
    return json.loads(raw)


def gh(*args: str) -> str:
    result = subprocess.run(["gh", *args], capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise RuntimeError(f"gh {' '.join(args[:3])}: {result.stderr.strip()[:160]}")
    return result.stdout


def fetch_patches(repo: str, number: int) -> dict[str, str]:
    """Per-file patch text for one pull request, keyed by path.

    Split here rather than handed to the diff parser whole because a case is
    about one file, and the validator's prompt should carry that file's hunks
    rather than a hundred others'.
    """
    diff = gh("pr", "diff", str(number), "--repo", repo)
    out: dict[str, str] = {}
    path, buf = None, []
    for line in diff.splitlines():
        header = _DIFF_GIT.match(line)
        if header:
            if path is not None:
                out[path] = "\n".join(buf)
            path, buf = header.group(1), [line]
            continue
        if path is not None:
            buf.append(line)
    if path is not None:
        out[path] = "\n".join(buf)
    return out


def stratified(rows: list[dict], n: int, seed: int) -> list[dict]:
    """Sample evenly across the axes that change what the validator has to do.

    Label, context level, and language each move the difficulty independently:
    a wrong comment is a different test from a right one, a repo-level claim
    cannot be settled from the diff, and a language with no rule pack exercises
    only the general rules. A uniform sample would over-weight whatever the
    dataset happens to contain most of.
    """
    buckets: dict[tuple, list[dict]] = defaultdict(list)
    for row in rows:
        buckets[(row.get("label"), row.get("context"),
                 row.get("project_main_language"))].append(row)

    rng = random.Random(seed)
    for group in buckets.values():
        rng.shuffle(group)

    out: list[dict] = []
    # Shuffled, not sorted. Round-robin over sorted keys hands a small sample
    # entirely to whichever bucket sorts first — asking for eight cases
    # returned eight incorrect ones, because "0" precedes "1".
    keys = sorted(buckets, key=lambda k: str(k))
    rng.shuffle(keys)
    while len(out) < n and any(buckets[k] for k in keys):
        for key in keys:
            if not buckets[key]:
                continue
            out.append(buckets[key].pop())
            if len(out) >= n:
                break
    return out


def to_case(row: dict, patch: str) -> dict | None:
    note = (row.get("note") or "").strip()
    path = row.get("path") or ""
    if not (note and path and patch):
        return None

    category = CATEGORY.get(row.get("category") or "", "logic")
    start = row.get("from_line") or row.get("to_line")
    end = row.get("to_line") or start
    if not isinstance(start, int):
        return None
    # `side` is which half of the diff the comment hangs off; ours is a
    # three-state tag and the dataset is 99% right-side.
    state = "diff-removed" if row.get("side") == "left" else "diff-added"
    lines = sorted({start, end}) if isinstance(end, int) else [start]

    return {
        "id": f"{row.get('pr_url','').rsplit('/',1)[-1]}-{path.rsplit('/',1)[-1]}-{start}",
        "expected_correct": bool(row.get("label")),
        "pr_url": row.get("pr_url", ""),
        "language": row.get("project_main_language", ""),
        "context_level": row.get("context", ""),
        "source_model": row.get("source_model") or "human",
        "is_ai_comment": bool(row.get("is_ai_comment")),
        "filepath": path,
        "patch": patch,
        "comment": {
            "message": note,
            "category": category,
            "severity": SEVERITY.get(category, "warning"),
            "lines": lines,
            "line_number_state": state,
            "criteria": f"aacr-{(row.get('category') or 'unknown').lower().replace(' ', '-')}",
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--cache", type=Path, default=Path("aacr_raw.json"))
    parser.add_argument("--sample", type=int, default=None,
                        help="Stratified sample size. Omit for everything, which "
                             "means fetching a patch for every distinct pull request.")
    parser.add_argument("--language", action="append",
                        help="Restrict to a project language. Repeatable.")
    parser.add_argument("--context", action="append",
                        choices=["Diff Level", "File Level", "Repo Level"],
                        help="Restrict to a context level. 'Diff Level' needs no "
                             "checkout, so it is the cheap subset to start with.")
    parser.add_argument("--ai-only", action="store_true",
                        help="Keep only machine-written comments — the distribution "
                             "this pipeline's own output falls into.")
    parser.add_argument("--seed", type=int, default=17)
    args = parser.parse_args()

    rows = load_dataset(args.cache)
    if args.language:
        rows = [r for r in rows if r.get("project_main_language") in args.language]
    if args.context:
        rows = [r for r in rows if r.get("context") in args.context]
    if args.ai_only:
        rows = [r for r in rows if r.get("is_ai_comment")]
    if not rows:
        print("no records match those filters", file=sys.stderr)
        return 1
    if args.sample:
        rows = stratified(rows, args.sample, args.seed)

    # One fetch per pull request, not per comment: the dataset puts many
    # comments on the same one.
    by_pr: dict[tuple, list[dict]] = defaultdict(list)
    for row in rows:
        match = _PR_URL.match(row.get("pr_url") or "")
        if match:
            by_pr[(match.group(1), int(match.group(2)))].append(row)

    cases, skipped, missing = [], 0, 0
    for (repo, number), group in sorted(by_pr.items(), key=lambda kv: str(kv[0])):
        try:
            patches = fetch_patches(repo, number)
        except Exception as exc:
            print(f"  skip {repo}#{number}: {exc}", file=sys.stderr)
            skipped += len(group)
            continue
        made = 0
        for row in group:
            patch = patches.get(row.get("path") or "", "")
            if not patch:
                missing += 1
                continue
            case = to_case(row, patch)
            if case is None:
                skipped += 1
                continue
            cases.append(case)
            made += 1
        print(f"  {repo}#{number:<7} {made:>3}/{len(group)} case(s)", file=sys.stderr)

    args.out.write_text(
        json.dumps({"version": 1, "source": "Alibaba-Aone/aacr-bench",
                    "cases": cases}, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    correct = sum(1 for c in cases if c["expected_correct"])
    print(f"\n{len(cases)} case(s) -> {args.out}  "
          f"(correct {correct}, incorrect {len(cases)-correct}; "
          f"{missing} file(s) not in the diff, {skipped} unusable)", file=sys.stderr)
    print("  " + "  ".join(f"{k}={v}" for k, v in
          Counter(c["context_level"] for c in cases).most_common()), file=sys.stderr)
    return 0 if cases else 1


if __name__ == "__main__":
    raise SystemExit(main())
