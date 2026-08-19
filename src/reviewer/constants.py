"""Tuning constants.

Values carried over from the reference implementation are marked as such; they
were measured against a different model and corpus, so treat them as starting
points to re-measure once a benchmark corpus exists.
"""

from __future__ import annotations

# --- Model ---------------------------------------------------------------

DEFAULT_MODEL = "deepseek-v4-pro"
# Strict function schemas require the beta endpoint.
DEFAULT_BASE_URL = "https://api.deepseek.com/beta"

# --- Ensemble ------------------------------------------------------------

# Reference impl uses 3. We default to 1: single-pass first, turn the ensemble
# on only once a benchmark can prove the 3x cost buys accuracy.
ENSEMBLE_SIZE = 1
ENSEMBLE_TIMEOUT_S = 1200

# --- Agentic loops -------------------------------------------------------

MAX_TURNS_REVIEW = 20
MAX_TURNS_VALIDATE = 20
TOOL_OUTPUT_CHAR_LIMIT = 40_000

# --- Chunking ------------------------------------------------------------

# v4-pro exposes a 1M context, so chunking is a safety net for pathological
# files rather than a routine step.
CHUNK_TOKEN_BUDGET = 120_000

# Per-request ceiling. Generous for a long review, but far below the old 600s:
# httpx times out on a monotonic clock that does not advance while the host
# sleeps, so an over-long ceiling turns a laptop suspend into an indefinite hang
# on a dead socket rather than a retry.
REQUEST_TIMEOUT_S = 180.0

# --- Retries -------------------------------------------------------------

MAX_PARSE_RETRIES = 3
MAX_DEGENERATE_RETRIES = 3
MAX_TRANSPORT_RETRIES = 6
MAX_CAPACITY_RETRIES = 10
BACKOFF_BASE_S = 2.0
BACKOFF_CAP_S = 60.0
CAPACITY_BACKOFF_S = 30.0

# --- Filtering -----------------------------------------------------------

REVIEW_IGNORE = (
    ".git/",
    ".gitkeep",
    "uv.lock",
    "poetry.lock",
    "package-lock.json",
    "pnpm-lock.yaml",
    "yarn.lock",
    "go.sum",
    "*.whl",
    "*.jar",
    "*.class",
    "*.png",
    "*.jpg",
    "*.jpeg",
    "*.gif",
    "*.svg",
    "*.ico",
    "*.pdf",
    "*.min.js",
    "*.min.css",
    "*.pb.go",
    "*_pb2.py",
    "*.generated.*",
)

# Fingerprint stamped into posted comments so stale reports can be found.
REPORT_FINGERPRINT = "<!-- pr-reviewer:code_review_report -->"
