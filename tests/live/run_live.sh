#!/usr/bin/env bash
# One-shot: start Jenkins in Docker, seed jobs, run the live test. Run from anywhere.
set -euo pipefail
cd "$(dirname "$0")"
PY="../../.venv/bin/python"
[ -x "$PY" ] || { echo "Run tests/macos/setup.sh first (.venv missing)"; exit 1; }
docker compose up -d --build
"$PY" setup_jobs.py http://localhost:8080
"$PY" live_test.py http://localhost:8080
