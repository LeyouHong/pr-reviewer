#!/usr/bin/env bash
# All offline. No network, no model calls, no token spend.
#
# `set -e` plus an explicit success line at the end: a suite that fails on an
# import error prints no FAIL, and grepping for one reads a broken run as a
# clean one. That happened.
set -e
PY="${PY:-.venv/bin/python}"
for suite in smoke_offline smoke_pipeline smoke_benchmark smoke_serve; do
    printf '%-18s' "$suite"
    "$PY" "tests/$suite.py" > /tmp/suite.out 2>&1 || {
        echo "FAILED"; cat /tmp/suite.out; exit 1;
    }
    tail -1 /tmp/suite.out
done
echo "── all suites passed ──"
