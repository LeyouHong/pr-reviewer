#!/usr/bin/env bash
# 全部离线，不联网、不消耗 token
set -e
PY="${PY:-.venv/bin/python}"
"$PY" tests/smoke_offline.py
"$PY" tests/smoke_pipeline.py | tail -1
"$PY" tests/smoke_benchmark.py | tail -1
"$PY" tests/smoke_serve.py | tail -1
