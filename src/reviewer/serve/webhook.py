"""GitHub webhook receiver.

Does three things and nothing else: verify the signature, put the revision on
the queue, return 200. GitHub gives up on a delivery after ten seconds and a
review takes minutes, so anything more here turns every slow review into a
failed delivery.

Signature verification is not optional. The endpoint's whole job is to make a
server start work on a repository, and without HMAC anyone who finds the URL
can point it at anything.

Delivery is best-effort by design — GitHub retries some failures and not
others, and a receiver that is down for a deploy misses events outright. The
reconciling sweep is what closes that gap; this is the fast path, not the
guarantee.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .queue import Job, JobQueue

log = logging.getLogger(__name__)

# The actions that mean "there is a revision nobody has reviewed".
# `synchronize` is a push to the branch and is the one that matters daily.
REVIEWABLE_ACTIONS = frozenset(
    {"opened", "synchronize", "reopened", "ready_for_review"}
)

_MAX_BODY_BYTES = 25 * 1024 * 1024  # GitHub's own payload ceiling


def verify_signature(secret: str, body: bytes, header: str | None) -> bool:
    """Constant-time check of GitHub's ``X-Hub-Signature-256``.

    An unsigned delivery fails when a secret is configured. Comparing with
    ``==`` would leak the expected digest a byte at a time to anyone able to
    measure the response.
    """
    if not header or not header.startswith("sha256="):
        return False
    digest = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(f"sha256={digest}", header)


def job_from_payload(payload: dict, *, delivery: str = "") -> Job | None:
    """The revision this event is about, or ``None`` when it is not our business."""
    action = payload.get("action")
    if action not in REVIEWABLE_ACTIONS:
        return None
    pull = payload.get("pull_request") or {}
    if pull.get("draft") and action != "ready_for_review":
        return None

    head = (pull.get("head") or {}).get("sha") or ""
    number = pull.get("number")
    repo = ((payload.get("repository") or {}).get("full_name")) or ""
    if not (head and number and repo):
        # Without all three there is no idempotency key, so accepting it would
        # mean a review that could be posted twice.
        log.warning("webhook: delivery %s lacks repo/number/sha; ignored", delivery)
        return None

    return Job(repo=repo, number=int(number), head_sha=head, reason=f"webhook:{action}")


class _Handler(BaseHTTPRequestHandler):
    server_version = "pr-reviewer"
    secret: str = ""
    queue: JobQueue | None = None
    path_prefix: str = "/webhook"

    def log_message(self, fmt: str, *args) -> None:  # noqa: A003
        log.debug("webhook: " + fmt, *args)

    def _reply(self, code: int, body: str) -> None:
        payload = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self) -> None:  # noqa: N802 - required name
        if self.path.rstrip("/") == "/health":
            depth = self.queue.depth() if self.queue else {}
            self._reply(200, json.dumps({"ok": True, "queue": depth}))
        else:
            self._reply(404, "not found")

    def do_POST(self) -> None:  # noqa: N802 - required name
        if self.path.rstrip("/") != self.path_prefix.rstrip("/"):
            self._reply(404, "not found")
            return

        length = int(self.headers.get("Content-Length") or 0)
        if length > _MAX_BODY_BYTES:
            self._reply(413, "payload too large")
            return
        body = self.rfile.read(length)
        delivery = self.headers.get("X-GitHub-Delivery", "")

        if self.secret and not verify_signature(
            self.secret, body, self.headers.get("X-Hub-Signature-256")
        ):
            log.warning("webhook: rejected delivery %s: bad signature", delivery)
            self._reply(401, "bad signature")
            return

        event = self.headers.get("X-GitHub-Event", "")
        if event == "ping":
            self._reply(200, "pong")
            return
        if event != "pull_request":
            self._reply(200, f"ignored event {event}")
            return

        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            self._reply(400, "invalid json")
            return

        job = job_from_payload(payload, delivery=delivery)
        if job is None:
            self._reply(200, "no reviewable revision in this event")
            return

        # Accepting means "it is on disk". The review happens elsewhere; saying
        # anything about its outcome here would mean holding the connection
        # open for minutes.
        accepted = self.queue.enqueue(job)
        self._reply(202, "queued" if accepted else "already known")


def serve(
    queue: JobQueue,
    *,
    host: str = "127.0.0.1",
    port: int = 8787,
    secret: str = "",
    path: str = "/webhook",
) -> None:
    """Run the receiver until interrupted."""
    if not secret:
        log.warning(
            "webhook: no secret configured — every caller is trusted. Set one "
            "before exposing this port beyond localhost."
        )
    handler = type(
        "BoundHandler",
        (_Handler,),
        {"secret": secret, "queue": queue, "path_prefix": path},
    )
    httpd = ThreadingHTTPServer((host, port), handler)
    log.info("webhook: listening on http://%s:%d%s", host, port, path)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        log.info("webhook: shutting down")
    finally:
        httpd.server_close()


__all__ = ["REVIEWABLE_ACTIONS", "job_from_payload", "serve", "verify_signature"]
