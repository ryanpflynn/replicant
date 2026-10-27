#!/usr/bin/env bash
# E2E-01-v0 from a clean slate: down -v, up --profile v0, seed, driver.
# Usage: tests/e2e/run_clean.sh [replicant dev up options, e.g. --no-build --with-observability --gateway passthrough]
set -euo pipefail
cd "$(dirname "$0")/../.."
uv run replicant dev down -v
uv run replicant dev up --profile v0 --timeout 1500 "$@"
uv run replicant dev seed
E2E_JOB_TIMEOUT_S=${E2E_JOB_TIMEOUT_S:-1200} uv run --package replicant-e2e python tests/e2e/e2e01_v0.py
