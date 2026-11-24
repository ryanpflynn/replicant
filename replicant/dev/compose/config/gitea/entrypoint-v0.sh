#!/bin/sh
# gitea with its CLI-only bootstrap folded in (SIMPLIFY-D3; was the gitea-bootstrap one-shot). Starts the image's own
# entrypoint, waits for the API, then runs the spec bootstrap.sh (site admin `gitea-admin`, user `replicant-bot`;
# idempotent) and touches /tmp/replicant-bootstrap.done, which the healthcheck requires. Everything that goes through
# the API (alice, org, team, sample repos, label, org webhook) is done by the `init` one-shot.
set -eu
rm -f /tmp/replicant-bootstrap.done            # a restarted container bootstraps (and reports healthy) again
/usr/local/bin/docker-entrypoint.sh "$@" &
pid=$!
trap 'kill -TERM "$pid" 2>/dev/null' TERM INT
i=0
until wget -q -O /dev/null http://127.0.0.1:3300/api/healthz 2>/dev/null; do
  kill -0 "$pid" 2>/dev/null || { wait "$pid"; exit $?; }
  i=$((i+1)); [ "$i" -gt 180 ] && { echo "gitea not healthy"; exit 1; }; sleep 1
done
/bin/sh /scripts/bootstrap.sh
touch /tmp/replicant-bootstrap.done
wait "$pid"
