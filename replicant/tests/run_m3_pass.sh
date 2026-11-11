#!/usr/bin/env bash
# One M3 stability pass from a clean slate: down -v, up --profile v0 [extra up options], seed, E2E-01-v0, the
# SEC-EGRESS suite (runner-contract driver, v0 mode) and the X0-7 chaos cases. Ends with fstrim inside the Podman
# VM so the VM's sparse disk image gives space back to the host.
#   tests/run_m3_pass.sh [--no-build] [--with-observability]
set -euo pipefail
cd "$(dirname "$0")/.."
X08=0; for a in "$@"; do [ "$a" = --with-observability ] && X08=1; done
export E2E_REQUIRE_X08=$X08
tests/e2e/run_clean.sh "$@"                       # down -v, up, seed, E2E-01-v0 (X0-8 asserted when Loki is up)
tests/runner-contract/run-v0.sh --mode contract --only SEC-EGRESS
uv run --package replicant-e2e python tests/chaos/chaos_v0.py
if [ "${REPLICANT_CONTAINER_CLI:-podman}" = podman ]; then podman machine ssh "sudo fstrim -a" >/dev/null 2>&1 || true; fi
echo "M3 PASS OK"
