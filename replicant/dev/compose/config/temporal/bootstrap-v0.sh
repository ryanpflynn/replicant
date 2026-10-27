#!/bin/sh
# v0 temporal-bootstrap: the spec bootstrap (namespace + C-IDS §4.1 search attributes), then checks that the
# Worker Deployment APIs work on the dev server, and prints the C-IDS §4 task queues. Idempotent.
set -eu
/bin/sh /scripts/bootstrap.sh
NS="${REPLICANT_TEMPORAL_NAMESPACE:-replicant-local}"
A="--address ${TEMPORAL_ADDRESS:-temporal:7233}"
temporal worker deployment list -n "$NS" $A >/dev/null || { echo "worker deployment API unavailable"; exit 1; }
echo "worker deployment API ok"
for q in replicant-jobs replicant-runner-calls replicant-publisher replicant-projections replicant-eval replicant-retention replicant-ops; do
  echo "task queue $q (created on first poll)"
done
