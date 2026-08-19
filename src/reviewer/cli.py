"""Command line entrypoints."""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

from .config import Config
from .pipeline.orchestrator import ReviewPipeline
from .benchmark.capture import capture
from .benchmark.calibration import run_calibration
from .benchmark.runner import run_benchmark
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
        "--ensemble",
        type=int,
        default=None,
        help="Number of independent reviewers per file; >1 enables majority rule.",
    )
    parser.add_argument(
        "--no-validate",
        action="store_true",
        help="Skip the qualification/validation pipeline (faster, noisier).",
    )
    parser.add_argument("--max-files", type=int, default=None)
    parser.add_argument("-o", "--output", type=Path, default=None)
    parser.add_argument("-v", "--verbose", action="store_true")


def _config_from(args: argparse.Namespace) -> Config:
    return Config.from_env(
        repo_path=args.repo_path.resolve(),
        model=args.model,
        api_key=args.api_key,
        base_url=args.base_url,
        ensemble_size=args.ensemble,
        enable_validation=False if args.no_validate else None,
        max_files=args.max_files,
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
    info = github.load_pull_request(args.number, args.repo)
    if not info.changes:
        print("No reviewable changes.", file=sys.stderr)
        return 0

    pipeline = ReviewPipeline(config)
    review = pipeline.run(info)
    markdown = pipeline.render(review, commit=os.environ.get("COMMIT_HASH", ""))

    if args.post:
        pruned = github.prune_old_reports(
            args.number, keep=max(args.max_reviews - 1, 0), repo=args.repo
        )
        if pruned:
            print(f"pruned {pruned} stale report(s)", file=sys.stderr)
        github.post_report(args.number, markdown, repo=args.repo)
        print(f"posted review to PR #{args.number}", file=sys.stderr)
    else:
        _emit(markdown, args.output)
    return 1 if review.error_count else 0


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


def cmd_calibrate_matcher(args: argparse.Namespace) -> int:
    import importlib.util

    spec = importlib.util.spec_from_file_location("calibration_cases", args.cases)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    config = _config_from(args)
    results, report = run_calibration(config, module.CALIBRATION_CASES)
    _emit(report, args.output)
    return 0 if all(r.passed for r in results) else 1


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
    pr.add_argument("--max-reviews", type=int, default=5)
    _common(pr)
    pr.set_defaults(func=cmd_review_pr)

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

    cal = sub.add_parser(
        "calibrate-matcher",
        help="Check the benchmark matcher against hand-written cases",
    )
    cal.add_argument("--cases", type=Path, default=Path("tests/calibration_cases.py"))
    _common(cal)
    cal.set_defaults(func=cmd_calibrate_matcher)

    args = parser.parse_args(argv)
    _setup_logging(args.verbose)
    try:
        return args.func(args)
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
