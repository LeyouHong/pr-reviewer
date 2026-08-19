"""Generate a corpus draft from a repo's fix commits.

Scans ``git log`` for commits whose message starts with ``fix``, reverses each
one's diff, and emits a ``corpus.json`` where the "PR under review" is a
proposed reintroduction of a bug that was previously fixed. Ground truth is
the fix commit's own message — the reviewer catches the reintroduction when
its finding matches what the fix originally addressed.

Every emitted CorpusPr has ``labelled=false`` on purpose. A human still has to
open ``corpus.json``, read each ground-truth entry, and decide whether the
commit message is specific enough to match against — many fix commits are
"address MR review errors" or "small refactor" that carry no attackable
description. Set ``labelled=true`` on the entries that pass the read.
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import subprocess
import sys
from pathlib import Path

log = logging.getLogger("corpus_from_fixes")

# Skip commits whose message is too generic to score against. A ground truth
# has to name a specific construct and failure mode — otherwise the judge
# matches nothing legitimately.
_TOO_GENERIC_PATTERNS = (
    re.compile(r"^fix:?\s+(minor|small|typo|formatting|lint|style)\b", re.IGNORECASE),
    re.compile(r"^fix\(review\):\s+address\b", re.IGNORECASE),
    re.compile(r"^fix:?\s+.{0,15}$", re.IGNORECASE),  # very short bodies
)

# Files whose diffs are noise for a code reviewer.
_SKIP_FILE_PATTERNS = re.compile(
    r"(?:^|/)(?:package(?:-lock)?\.json|yarn\.lock|go\.sum|.*\.min\.[jc]ss?|"
    r"CHANGELOG.*|.*\.snap|.*\.png|.*\.jpg|.*\.gif|.*\.svg|.*\.pdf|"
    r"i18n/.*\.(?:json|properties)|.*_pb2\.py|.*\.pb\.go)$"
)


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout


def _select_commits(repo: Path, base_ref: str, limit: int) -> list[str]:
    """Return SHA list of candidate fix commits, newest first.

    ``--no-merges`` because a merge commit's diff spans the whole feature
    branch, not the one bug being fixed — the ground truth would be too
    diffuse to score against.
    """
    raw = _git(
        repo,
        "log",
        "--no-merges",
        "--extended-regexp",
        "--grep=^fix",
        "--pretty=format:%H%x00%s",
        base_ref,
    )
    picks: list[str] = []
    for line in raw.splitlines():
        sha, _, subject = line.partition("\x00")
        if not sha:
            continue
        if any(p.search(subject) for p in _TOO_GENERIC_PATTERNS):
            log.debug("skip too-generic %s: %s", sha[:8], subject)
            continue
        picks.append(sha)
        if len(picks) >= limit:
            break
    return picks


def _commit_meta(repo: Path, sha: str) -> tuple[str, str, str]:
    """Return (subject, body, iso_date)."""
    raw = _git(repo, "show", "-s", "--pretty=format:%s%x1f%b%x1f%aI", sha)
    subject, body, date = raw.split("\x1f", 2)
    return subject.strip(), body.strip(), date.strip()


def _reversed_diff(repo: Path, sha: str) -> str:
    """Get the diff that reintroduces the bug this commit fixed.

    ``git show -R`` reverses the patch; we then filter out lockfiles and
    generated files that only add noise to a review prompt.
    """
    raw = _git(repo, "show", "-R", "--format=", sha)
    # Split by file so we can drop the noisy ones.
    parts = _split_by_file(raw)
    kept: list[str] = []
    for part in parts:
        filepath = _extract_new_path(part)
        if not filepath or _SKIP_FILE_PATTERNS.search(filepath):
            continue
        kept.append(part)
    return "".join(kept)


def _split_by_file(diff: str) -> list[str]:
    parts: list[str] = []
    current: list[str] = []
    for line in diff.splitlines(keepends=True):
        if line.startswith("diff --git ") and current:
            parts.append("".join(current))
            current = [line]
        else:
            current.append(line)
    if current:
        parts.append("".join(current))
    return parts


def _extract_new_path(part: str) -> str:
    # ``git show -R`` swaps the ``a/`` and ``b/`` prefixes, so the new-side
    # marker can carry either. Strip whichever appears.
    for line in part.splitlines():
        if line.startswith("+++ /dev/null") or not line.startswith("+++ "):
            continue
        path = line[4:]
        if path.startswith("b/") or path.startswith("a/"):
            path = path[2:]
        return path
    return ""


def _files_touched(diff: str) -> list[str]:
    files: list[str] = []
    for part in _split_by_file(diff):
        path = _extract_new_path(part)
        if path:
            files.append(path)
    return files


def _mr_number(subject: str, body: str) -> str:
    """Try to recover the source MR number from the message ('!12345')."""
    match = re.search(r"!(\d{2,6})", subject + " " + body)
    return match.group(1) if match else ""


def _ground_truth_entry(sha: str, subject: str, body: str, files: list[str]) -> dict:
    # Description leads with the commit subject (minus the type prefix) and
    # falls back to the body when the subject is generic. Anchor the file to
    # the first touched path — a lot of fix commits touch a single file, and
    # for the multi-file ones this at least points a human labeller at where
    # to start editing.
    lead = re.sub(r"^fix(?:\([^)]*\))?:\s*", "", subject).strip() or subject
    description = lead
    if body:
        # Include the first paragraph of the body if it adds anything.
        body_head = body.split("\n\n", 1)[0].strip()
        if body_head and body_head.lower() not in description.lower():
            description = f"{description} — {body_head}"

    return {
        "id": f"{sha[:8]}-1",
        "file": files[0] if files else "",
        "lines": [],  # left blank; labeller fills in from the reversed diff
        "description": description,
        "min_severity": "error",
        "value": "p2",   # conservative default; labeller can promote to p1
        "requires_exploration": False,
        "note": (
            f"AUTO-GENERATED from commit {sha}. Rewrite the description to "
            "name the specific construct and failure mode; the raw commit "
            "message is often not attackable on its own. Set labelled=true "
            "on the CorpusPr once this entry is human-reviewed."
        ),
    }


def build_corpus(
    repo: Path, base_ref: str, limit: int, repo_slug: str
) -> dict:
    shas = _select_commits(repo, base_ref, limit)
    log.info("selected %d candidate fix commits", len(shas))

    prs = []
    for sha in shas:
        subject, body, date = _commit_meta(repo, sha)
        diff = _reversed_diff(repo, sha)
        if not diff.strip():
            log.info("skip %s: no interesting files after filter", sha[:8])
            continue
        files = _files_touched(diff)
        mr_num = _mr_number(subject, body)

        pr = {
            "id": f"{repo_slug}#{sha[:8]}",
            "repo": repo_slug,
            "number": int(mr_num) if mr_num else 0,
            "title": subject,
            "description": (
                f"Retrospective corpus entry: this diff reintroduces the bug "
                f"that commit {sha} fixed on {date}. A working reviewer flags "
                f"the reintroduction."
                + (f" Originally from MR !{mr_num}." if mr_num else "")
            ),
            "base_branch": base_ref,
            "head_branch": f"revert-{sha[:8]}",
            "pin_commit": sha,
            "diff": diff,
            "ground_truth": [_ground_truth_entry(sha, subject, body, files)],
            "labelled": False,
        }
        prs.append(pr)

    return {"version": 1, "prs": prs}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--repo",
        type=Path,
        required=True,
        help="Path to the git repository to scan.",
    )
    parser.add_argument(
        "--base-ref",
        default="Dev_ncm_8.0",
        help="Branch whose fix commits to sample. Default: Dev_ncm_8.0",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=30,
        help="Cap on candidate commits. Fortinac's own baseline is ~30 MRs.",
    )
    parser.add_argument(
        "--repo-slug",
        default="fortinet/FortiNAC",
        help="Display name written into each CorpusPr.repo.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("corpus.json"),
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)-7s %(message)s",
        stream=sys.stderr,
    )

    corpus = build_corpus(args.repo, args.base_ref, args.limit, args.repo_slug)
    args.output.write_text(json.dumps(corpus, indent=2, ensure_ascii=False), encoding="utf-8")
    print(
        f"wrote {args.output} with {len(corpus['prs'])} candidate PR(s). "
        f"Every entry has labelled=false — read them before scoring.",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
