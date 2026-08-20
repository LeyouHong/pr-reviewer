"""Queue and receiver semantics. No network, no model calls."""
import hashlib
import hmac
import json
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from reviewer.serve.queue import Job, JobQueue
from reviewer.serve.webhook import job_from_payload, verify_signature

F = []
def check(n, c, d=""):
    print(("  PASS  " if c else "  FAIL  ") + n + (f"  {d}" if not c else ""))
    if not c: F.append(n)

def q():
    return JobQueue(Path(tempfile.mkdtemp()))

def job(sha, number=7, repo="o/r"):
    return Job(repo=repo, number=number, head_sha=sha)

print("\n[queue] identity is the revision")
Q = q()
check("first offer accepted", Q.enqueue(job("a" * 40)))
check("same revision offered twice is accepted once", not Q.enqueue(job("a" * 40)))
check("a different pull request is independent", Q.enqueue(job("a" * 40, number=8)))
check("a different repo is independent", Q.enqueue(job("a" * 40, repo="o/other")))

print("\n[queue] a newer revision supersedes an older pending one")
Q = q()
Q.enqueue(job("a" * 40)); Q.enqueue(job("b" * 40)); Q.enqueue(job("c" * 40))
check("three pushes leave one job", Q.depth()["pending"] == 1, Q.depth())
claimed = Q.claim()
check("the surviving job is the newest revision", claimed[0].head_sha == "c" * 40)

print("\n[queue] claiming is exclusive and survives a crash")
Q = q()
Q.enqueue(job("a" * 40))
first = Q.claim()
check("a claimed job is not claimable again", Q.claim() is None)
check("claim moves it out of pending", Q.depth()["pending"] == 0, Q.depth())
Q.complete(first[0], first[1])
check("completion clears the claim", Q.depth() == {"pending": 0, "claimed": 0, "done": 1, "failed": 0}, Q.depth())
check("a completed revision is not re-queued", not Q.enqueue(job("a" * 40)))

Q = q(); Q.claim_ttl_s = 0.05
Q.enqueue(job("d" * 40))
abandoned = Q.claim()          # worker "dies" holding it
time.sleep(0.1)
recovered = Q.claim()
check("a job abandoned by a dead worker is reclaimed",
      recovered is not None and recovered[0].head_sha == "d" * 40)

print("\n[queue] failure handling")
Q = q(); Q.enqueue(job("e" * 40))
j, p = Q.claim(); Q.requeue(j, p)
check("requeue counts the attempt", Q.claim()[0].attempts == 1)
Q = q(); Q.enqueue(job("f" * 40))
j, p = Q.claim(); Q.fail(j, p, "boom")
check("failure records the error", json.loads(
    next((Path(Q.root) / "failed").glob("*.json")).read_text())["error"] == "boom")
check("a failed revision can be offered again", Q.enqueue(job("f" * 40)))

print("\n[webhook] signatures")
body, secret = b'{"a":1}', "s3cret"
good = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
check("a correct signature passes", verify_signature(secret, body, good))
check("a wrong signature fails", not verify_signature(secret, body, "sha256=" + "0" * 64))
check("a missing signature fails", not verify_signature(secret, body, None))
check("an unprefixed digest fails", not verify_signature(secret, body, good.split("=", 1)[1]))
check("a signature for other content fails", not verify_signature(secret, b'{"a":2}', good))

print("\n[webhook] which events carry a revision")
def ev(action, sha="abc123", draft=False, repo="o/r", number=7):
    pull = {"number": number, "draft": draft, "head": {"sha": sha}}
    return {"action": action, "pull_request": pull, "repository": {"full_name": repo}}

for action in ("opened", "synchronize", "reopened", "ready_for_review"):
    check(f"{action} yields a job", job_from_payload(ev(action)) is not None)
for action in ("closed", "labeled", "assigned", "edited"):
    check(f"{action} yields nothing", job_from_payload(ev(action)) is None)
check("a draft is left alone until it is ready",
      job_from_payload(ev("synchronize", draft=True)) is None)
check("marking a draft ready does review it",
      job_from_payload(ev("ready_for_review", draft=True)) is not None)
check("a payload without a head sha is ignored, not guessed",
      job_from_payload({"action": "opened", "pull_request": {"number": 7, "head": {}},
                        "repository": {"full_name": "o/r"}}) is None)
j = job_from_payload(ev("synchronize"))
check("the job records why it exists", j.reason == "webhook:synchronize", j.reason)

print("\n[checkout] each job reads the revision it judges")
import subprocess
from reviewer.config import Config
from reviewer.sources.checkout import CheckoutProvider

def _git(repo, *a):
    return subprocess.run(["git", "-C", str(repo), *a], capture_output=True, text=True, check=True).stdout

clone = Path(tempfile.mkdtemp()) / "repo"
clone.mkdir()
_git(clone, "init", "-q", "-b", "main")
_git(clone, "config", "user.email", "t@t"); _git(clone, "config", "user.name", "t")
(clone / "a.txt").write_text("first\n")
_git(clone, "add", "-A"); _git(clone, "commit", "-qm", "first")
first = _git(clone, "rev-parse", "HEAD").strip()
(clone / "a.txt").write_text("second\n")
_git(clone, "add", "-A"); _git(clone, "commit", "-qm", "second")
second = _git(clone, "rev-parse", "HEAD").strip()

base = Config(api_key="x", enable_validation=True, agentic_review=True)

# An unconfigured repository must not fall back to the process cwd.
with CheckoutProvider().pinned(base, "o/unknown", 1, first) as cfg:
    check("no clone disables validation", cfg.enable_validation is False)
    check("no clone disables agentic review", cfg.agentic_review is False)
    check("no clone leaves repo_path untouched", cfg.repo_path == base.repo_path)

provider = CheckoutProvider({"o/r": clone})
try:
    with provider.pinned(base, "o/r", 7, first) as cfg:
        check("a configured repo keeps validation on", cfg.enable_validation is True)
        check("the worktree holds the job's own revision",
              (cfg.repo_path / "a.txt").read_text().strip() == "first",
              (cfg.repo_path / "a.txt").read_text())
        held = cfg.repo_path
    check("the worktree is released when the job ends", not held.exists())

    # The next job is a different revision — the whole reason a fixed
    # repo_path is wrong for a queue worker.
    with provider.pinned(base, "o/r", 8, second) as cfg:
        check("a later job sees its own revision, not the previous one",
              (cfg.repo_path / "a.txt").read_text().strip() == "second")

    with provider.pinned(base, "o/r", 9, "0" * 40) as cfg:
        check("an unknown commit degrades instead of guessing",
              cfg.enable_validation is False and cfg.agentic_review is False)
finally:
    provider.cleanup()
check("cleanup leaves no worktrees behind",
      "worktree" not in _git(clone, "worktree", "list").replace(str(clone), ""))

print()
if F:
    print(f"{len(F)} FAILED: {F}"); sys.exit(1)
print("serve checks passed")
