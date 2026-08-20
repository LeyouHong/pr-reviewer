"""Command line entrypoints."""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

from .config import Config
from .provider.profiles import PROFILES
from .pipeline.orchestrator import ReviewPipeline
from .benchmark.capture import capture
from .benchmark.calibration import run_calibration
from .benchmark.gold import format_worklist, label_run
from .benchmark.runner import run_benchmark
from .pipeline.inline import build_inline_review
from .pipeline.scan import LockHeld, scan_repos
from .serve.queue import JobQueue
from .serve.webhook import serve as serve_webhook
from .serve.worker import drain
from .settings import load_settings
from .sources import github, local_git


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(levelname)-7s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("openai").setLevel(logging.WARNING)


def _common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--repo-path", type=Path, default=Path.cwd())
    parser.add_argument("--model", default=None)
    parser.add_argument("--api-key", default=None)
    parser.add_argument("--base-url", default=None)
    parser.add_argument(
        "--provider",
        choices=sorted(PROFILES),
        default=None,
        help="Endpoint capability profile: how the server can be made to honour "
             "a schema, how much context it has, which vendor fields it takes. "
             "Use 'generic' for an unknown local server.",
    )
    parser.add_argument(
        "--ensemble",
        type=int,
        default=None,
        help="Number of independent reviewers per file; >1 enables majority rule.",
    )
    parser.add_argument(
        "--agentic",
        action="store_true",
        help="Let the reviewer read the repository while reviewing, instead of "
             "judging the diff in isolation.",
    )
    parser.add_argument(
        "--v2",
        action="store_true",
        help="Single-agent reviewer with tools (equivalent to --agentic --ensemble 1). "
             "Cheaper than an ensemble and, per the reference implementation, "
             "usually has a better false-positive rate — worth A/B'ing on your corpus.",
    )
    parser.add_argument(
        "--no-validate",
        action="store_true",
        help="Skip the qualification/validation pipeline (faster, noisier).",
    )
    parser.add_argument(
        "--semgrep",
        action="store_true",
        help="Run semgrep over the changed files and fold findings into the review.",
    )
    parser.add_argument("--max-files", type=int, default=None)
    parser.add_argument("-o", "--output", type=Path, default=None)
    parser.add_argument("-v", "--verbose", action="store_true")


def _config_from(args: argparse.Namespace) -> Config:
    # --v2 is a shorthand for the mode the reference implementation recommends
    # once ensemble no longer earns its 3x cost. It maps to --agentic
    # --ensemble 1 and, when the user set neither flag, wins the tie. An
    # explicit --ensemble N alongside --v2 stays as N so the user's override is
    # respected.
    agentic = args.agentic or args.v2
    ensemble = args.ensemble if args.ensemble is not None else (1 if args.v2 else None)
    return Config.from_env(
        repo_path=args.repo_path.resolve(),
        model=args.model,
        api_key=args.api_key,
        base_url=args.base_url,
        provider_profile=args.provider,
        agentic_review=True if agentic else None,
        ensemble_size=ensemble,
        enable_validation=False if args.no_validate else None,
        semgrep_enabled=True if args.semgrep else None,
        max_files=args.max_files,
        max_reviews=getattr(args, "max_reviews", None),
        output_path=args.output,
        verbose=args.verbose,
    )


def _emit(markdown: str, output: Path | None) -> None:
    if output:
        output.write_text(markdown, encoding="utf-8")
        print(f"wrote {output}", file=sys.stderr)
    else:
        print(markdown)


def cmd_review_diff(args: argparse.Namespace) -> int:
    config = _config_from(args)
    info = local_git.load_local_diff(config.repo_path, args.base)
    if not info.changes:
        print("No reviewable changes.", file=sys.stderr)
        return 0
    pipeline = ReviewPipeline(config)
    review = pipeline.run(info)
    _emit(pipeline.render(review, commit=os.environ.get("COMMIT_HASH", "")), args.output)
    return 1 if review.error_count else 0


def cmd_review_pr(args: argparse.Namespace) -> int:
    config = _config_from(args)
    info, _diff, head_sha = github.load_pull_request_raw(args.number, args.repo)
    if not info.changes:
        print("No reviewable changes.", file=sys.stderr)
        return 0

    pipeline = ReviewPipeline(config)
    review = pipeline.run(info)
    commit = os.environ.get("COMMIT_HASH", "")

    if args.post:
        pruned = github.prune_old_reports(
            args.number, keep=max(config.max_reviews - 1, 0), repo=args.repo
        )
        if pruned:
            print(f"pruned {pruned} stale report(s)", file=sys.stderr)
        if args.inline:
            payload = build_inline_review(review, commit=commit)
            comments = [
                {"path": c.path, "line": c.line, "side": c.side, "body": c.body}
                for c in payload.comments
            ]
            github.post_inline_review(
                args.number,
                head_sha,
                payload.body,
                comments,
                repo=args.repo,
            )
            print(
                f"posted inline review to PR #{args.number} "
                f"({len(comments)} inline, {len(payload.unmapped)} in body)",
                file=sys.stderr,
            )
        else:
            markdown = pipeline.render(review, commit=commit)
            github.post_report(args.number, markdown, repo=args.repo)
            print(f"posted review to PR #{args.number}", file=sys.stderr)
    else:
        markdown = pipeline.render(review, commit=commit)
        _emit(markdown, args.output)
    return 1 if review.error_count else 0


def cmd_scan(args: argparse.Namespace) -> int:
    settings = load_settings(args.settings)
    if not settings.repositories:
        print(f"No repositories configured in {args.settings}", file=sys.stderr)
        return 0
    config = _config_from(args)
    try:
        results = scan_repos(settings, config, dry_run=args.dry_run)
    except LockHeld as exc:
        # Exit 0: cron firing while the previous sweep still runs is the
        # system working, not a failure worth mailing the operator about.
        print(f"scan: another sweep is already running ({exc})", file=sys.stderr)
        return 0
    reviewed = sum(1 for r in results if r.get("reviewed"))
    skipped = len(results) - reviewed
    print(
        f"scan: reviewed {reviewed}, skipped {skipped} across "
        f"{len(settings.repositories)} repo(s)",
        file=sys.stderr,
    )
    return 0


def cmd_benchmark_capture(args: argparse.Namespace) -> int:
    corpus = capture(args.corpus, args.repo, args.numbers)
    unlabelled = [pr.id for pr in corpus.prs if not pr.labelled]
    print(f"corpus now holds {len(corpus.prs)} PR(s)", file=sys.stderr)
    if unlabelled:
        print(
            "Awaiting ground truth (add issues, then set labelled=true):\n  "
            + "\n  ".join(unlabelled),
            file=sys.stderr,
        )
    return 0


def cmd_benchmark_run(args: argparse.Namespace) -> int:
    config = _config_from(args)
    report = run_benchmark(config, args.corpus, args.run_dir, args.checkout_repo)
    _emit(report, args.output)
    return 0


def cmd_benchmark_gold(args: argparse.Namespace) -> int:
    config = _config_from(args)
    worklist = label_run(config, args.corpus, args.run_dir, passes=args.passes)
    args.worklist.parent.mkdir(parents=True, exist_ok=True)
    args.worklist.write_text(worklist.model_dump_json(indent=2), encoding="utf-8")
    _emit(format_worklist(worklist), args.output)
    unsettled = len(worklist.unsettled)
    print(
        f"gold: {len(worklist.confident)}/{len(worklist.findings)} confident, "
        f"{unsettled} TODO",
        file=sys.stderr,
    )
    return 0


def cmd_calibrate_matcher(args: argparse.Namespace) -> int:
    import importlib.util

    spec = importlib.util.spec_from_file_location("calibration_cases", args.cases)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    config = _config_from(args)
    results, report = run_calibration(config, module.CALIBRATION_CASES)
    _emit(report, args.output)
    return 0 if all(r.passed for r in results) else 1


def cmd_serve(args: argparse.Namespace) -> int:
    secret = args.secret or os.environ.get("GITHUB_WEBHOOK_SECRET", "")
    serve_webhook(
        JobQueue(args.queue), host=args.host, port=args.port,
        secret=secret, path=args.path,
    )
    return 0


def cmd_worker(args: argparse.Namespace) -> int:
    config = _config_from(args)
    queue = JobQueue(args.queue)
    handled = drain(config, queue, inline=not args.no_inline, once=args.once)
    print(f"worker: handled {handled} job(s)", file=sys.stderr)
    return 0


def cmd_queue(args: argparse.Namespace) -> int:
    depth = JobQueue(args.queue).depth()
    print("  ".join(f"{lane}={n}" for lane, n in depth.items()))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="pr-reviewer", description="LLM code review agent (DeepSeek V4 Pro)"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    diff = sub.add_parser("review-diff", help="Review a local branch against a base ref")
    diff.add_argument("--base", default="main")
    _common(diff)
    diff.set_defaults(func=cmd_review_diff)

    pr = sub.add_parser("review-pr", help="Review a GitHub pull request")
    pr.add_argument("number", type=int)
    pr.add_argument("--repo", default=None, help="owner/name; defaults to the cwd repo")
    pr.add_argument("--post", action="store_true", help="Post the report as a PR comment")
    pr.add_argument(
        "--inline",
        action="store_true",
        help="Post as a GitHub review with per-line comments instead of one "
             "aggregated issue comment. Requires --post.",
    )
    pr.add_argument(
        "--max-reviews",
        type=int,
        default=None,
        help="Reports to keep on the pull request (default 5).",
    )
    _common(pr)
    pr.set_defaults(func=cmd_review_pr)

    scan = sub.add_parser(
        "scan",
        help="Walk every repo in settings.json and review PRs that need attention",
    )
    scan.add_argument(
        "--settings",
        type=Path,
        default=Path("settings.json"),
        help="Path to the scan settings file.",
    )
    scan.add_argument(
        "--dry-run",
        action="store_true",
        help="Log the review/skip decision for each PR but do not post anything.",
    )
    _common(scan)
    scan.set_defaults(func=cmd_scan)

    cap = sub.add_parser("benchmark-capture", help="Add PRs to a corpus for labelling")
    cap.add_argument("--repo", required=True, help="owner/name")
    cap.add_argument("numbers", type=int, nargs="+", help="PR numbers to capture")
    cap.add_argument("--corpus", type=Path, default=Path("corpus.json"))
    cap.add_argument("-v", "--verbose", action="store_true")
    cap.set_defaults(func=cmd_benchmark_capture)

    bench = sub.add_parser("benchmark-run", help="Score the reviewer against a corpus")
    bench.add_argument("--corpus", type=Path, default=Path("corpus.json"))
    bench.add_argument("--run-dir", type=Path, default=Path("benchmark_runs/latest"))
    bench.add_argument(
        "--checkout-repo",
        type=Path,
        default=None,
        help="Clone of the reviewed repository. Each PR is checked out at its "
             "pin_commit in a worktree so the validator reads the code the labels "
             "were written against. Required for a comparable validated run.",
    )
    _common(bench)
    bench.set_defaults(func=cmd_benchmark_run)

    gold = sub.add_parser(
        "benchmark-gold",
        help="Run N judge passes over an existing benchmark run and merge them",
    )
    gold.add_argument("--corpus", type=Path, default=Path("corpus.json"))
    gold.add_argument("--run-dir", type=Path, default=Path("benchmark_runs/latest"))
    gold.add_argument(
        "--passes",
        type=int,
        default=3,
        help="Number of independent judge passes per finding. Odd is preferable.",
    )
    gold.add_argument(
        "--worklist",
        type=Path,
        default=Path("gold.json"),
        help="Where to write the merged worklist JSON.",
    )
    _common(gold)  # supplies -o/--output for the markdown summary.
    gold.set_defaults(func=cmd_benchmark_gold)

    cal = sub.add_parser(
        "calibrate-matcher",
        help="Check the benchmark matcher against hand-written cases",
    )
    cal.add_argument("--cases", type=Path, default=Path("tests/calibration_cases.py"))
    _common(cal)
    cal.set_defaults(func=cmd_calibrate_matcher)

    srv = sub.add_parser("serve", help="Receive GitHub webhooks and queue reviews")
    srv.add_argument("--queue", type=Path, default=Path(".pr-reviewer/queue"))
    srv.add_argument("--host", default="127.0.0.1")
    srv.add_argument("--port", type=int, default=8787)
    srv.add_argument("--path", default="/webhook")
    srv.add_argument(
        "--secret", default=None,
        help="Webhook HMAC secret (or GITHUB_WEBHOOK_SECRET). Without one every "
             "caller is trusted — required before exposing the port.",
    )
    srv.add_argument("-v", "--verbose", action="store_true")
    srv.set_defaults(func=cmd_serve)

    wk = sub.add_parser("worker", help="Review queued revisions and post the reports")
    wk.add_argument("--queue", type=Path, default=Path(".pr-reviewer/queue"))
    wk.add_argument("--once", action="store_true", help="Drain and exit.")
    wk.add_argument("--no-inline", action="store_true",
                    help="Post one aggregated comment instead of a line-anchored review.")
    _common(wk)
    wk.set_defaults(func=cmd_worker)

    qd = sub.add_parser("queue", help="Show queue depth")
    qd.add_argument("--queue", type=Path, default=Path(".pr-reviewer/queue"))
    qd.add_argument("-v", "--verbose", action="store_true")
    qd.set_defaults(func=cmd_queue)

    args = parser.parse_args(argv)
    _setup_logging(args.verbose)
    try:
        return args.func(args)
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
