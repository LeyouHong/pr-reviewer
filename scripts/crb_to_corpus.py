#!/usr/bin/env python3
"""Convert the Martian Code Review Bench gold set into a corpus.json.

Why bother when we already have a corpus: ours is sixteen defects in one
TypeScript repository, labelled by me — the same model family that then gets
measured against them. Recall computed on your own labels tells you how well
the reviewer agrees with itself. CRB's fifty pull requests carry 173
human-verified comments across Python, Go, TypeScript, Ruby and Java, none of
them written by this pipeline, which is the only way to find out whether the
three language rule packs that have never seen a real file actually work.

Source: https://github.com/withmartian/crb (MIT), offline/golden_comments/.

What does not survive the conversion, and why the scorecard says so:

* CRB records no file or line for a comment, so `requires_exploration` cannot
  be derived. It is written as False with a note, and the diff-only /
  cross-file recall split is meaningless on this corpus — read the global
  number instead.
* File paths are recovered by looking for a changed file's basename inside the
  comment text. A comment that names no file gets "(unspecified)"; the judge
  pairs on the description either way.

Usage:
    scripts/crb_to_corpus.py --out crb_corpus.json                # all 50
    scripts/crb_to_corpus.py --out one.json --limit 1             # smoke test
    scripts/crb_to_corpus.py --out crb.json --project sentry
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import urllib.request
from pathlib import Path

RAW = "https://raw.githubusercontent.com/withmartian/crb/main/offline/golden_comments"
PROJECTS = ["cal_dot_com", "discourse", "grafana", "keycloak", "sentry"]

# CRB grades impact; so does our value tier. Severity maps to the floor at
# which a reviewer finding still counts as catching the defect.
SEVERITY = {"critical": "error", "high": "error", "medium": "warning", "low": "info"}
VALUE = {"critical": "p1", "high": "p2", "medium": "p2", "low": "p3"}

# CRB's own label for a comment it does not stand behind. Excluded by default
# rather than silently kept: scoring a reviewer against a speculative label
# punishes it for declining to speculate.
DEFAULT_EXCLUDED = ("speculative",)


def fetch_golden(project: str) -> list[dict]:
    with urllib.request.urlopen(f"{RAW}/{project}.json", timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))


def gh(*args: str) -> str:
    result = subprocess.run(["gh", *args], capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise RuntimeError(f"gh {' '.join(args)}: {result.stderr.strip()[:200]}")
    return result.stdout


def parse_url(url: str) -> tuple[str, int]:
    match = re.match(r"https://github\.com/([^/]+/[^/]+)/pull/(\d+)", url.strip())
    if not match:
        raise ValueError(f"unrecognised pull request url: {url}")
    return match.group(1), int(match.group(2))


def locate(comment: str, changed: list[str]) -> str:
    """Best-effort file for a comment that names one, else "(unspecified)".

    Longest basename first, so a comment mentioning both `api.ts` and
    `organization_api.ts` resolves to the more specific of the two.
    """
    for path in sorted(changed, key=lambda p: -len(p.rsplit("/", 1)[-1])):
        if path.rsplit("/", 1)[-1] in comment:
            return path
    return "(unspecified)"


def convert_pr(record: dict, project: str, index: int, excluded: tuple[str, ...]) -> dict | None:
    repo, number = parse_url(record["url"])
    slug = f"{repo}#{number}"

    meta = json.loads(
        gh("pr", "view", str(number), "--repo", repo, "--json",
           "number,title,body,headRefName,baseRefName,headRefOid,files")
    )
    diff = gh("pr", "diff", str(number), "--repo", repo)
    if not diff.strip():
        print(f"  skip {slug}: empty diff", file=sys.stderr)
        return None

    changed = [f["path"] for f in meta.get("files") or []]
    ground_truth = []
    for i, comment in enumerate(record.get("comments") or [], start=1):
        category = str(comment.get("category", "")).lower()
        if category in excluded:
            continue
        severity = str(comment.get("severity", "medium")).lower()
        text = str(comment.get("comment", "")).strip()
        if not text:
            continue
        note = f"crb category={category or 'unknown'} severity={severity}; " \
               "requires_exploration not provided by CRB"
        if record.get("az_comment"):
            note += f"; crb az_comment={record['az_comment']!r}"
        ground_truth.append({
            "id": f"{project}-{number}-{i}",
            "file": locate(text, changed),
            "lines": [],
            "description": text,
            "min_severity": SEVERITY.get(severity, "warning"),
            "value": VALUE.get(severity, "p2"),
            "requires_exploration": False,
            "note": note,
        })

    return {
        "id": slug,
        "repo": repo,
        "number": number,
        "title": meta.get("title") or record.get("pr_title") or "",
        "description": meta.get("body") or "",
        "base_branch": meta.get("baseRefName") or "",
        "head_branch": meta.get("headRefName") or "",
        "pin_commit": meta.get("headRefOid") or "",
        "diff": diff,
        "ground_truth": ground_truth,
        "labelled": True,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--project", choices=PROJECTS, action="append",
                        help="Restrict to one or more CRB projects. Repeatable.")
    parser.add_argument("--limit", type=int, default=None,
                        help="Stop after N pull requests. Use 1 to smoke-test the chain.")
    parser.add_argument("--keep-category", action="append", default=[],
                        help=f"Keep a category excluded by default {DEFAULT_EXCLUDED}.")
    args = parser.parse_args()

    excluded = tuple(c for c in DEFAULT_EXCLUDED if c not in args.keep_category)
    projects = args.project or PROJECTS

    prs, taken = [], 0
    for project in projects:
        for index, record in enumerate(fetch_golden(project)):
            if args.limit is not None and taken >= args.limit:
                break
            try:
                converted = convert_pr(record, project, index, excluded)
            except Exception as exc:  # one unfetchable PR must not sink the set
                print(f"  skip {record.get('url')}: {exc}", file=sys.stderr)
                continue
            if converted is None:
                continue
            prs.append(converted)
            taken += 1
            print(f"  {converted['id']:<44} {len(converted['ground_truth']):>2} label(s)  "
                  f"pin {converted['pin_commit'][:8]}", file=sys.stderr)

    args.out.write_text(
        json.dumps({"version": 1, "prs": prs}, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    total = sum(len(p["ground_truth"]) for p in prs)
    print(f"\n{len(prs)} PR(s), {total} label(s) -> {args.out}", file=sys.stderr)
    return 0 if prs else 1


if __name__ == "__main__":
    raise SystemExit(main())
