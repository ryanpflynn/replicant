#!/usr/bin/env bash
# Throwaway Gitea (container wrk-gitea-<suffix>) with user replicant-bot, org acme-payments and repo ledger-api seeded
# with one commit. Prints "<port> <token>" (the token is a test-only credential of the throwaway container).
set -euo pipefail
ENGINE="${REPLICANT_CONTAINER_CLI:-podman}"
cmd=$1; C=$2
if [ "$cmd" = down ]; then "$ENGINE" rm -f -v "$C" >/dev/null 2>&1 || true; exit 0; fi
"$ENGINE" rm -f -v "$C" >/dev/null 2>&1 || true
"$ENGINE" run -d --name "$C" --memory 512m -p 127.0.0.1::3000 \
  -e GITEA__security__INSTALL_LOCK=true -e GITEA__database__DB_TYPE=sqlite3 -e GITEA__server__ROOT_URL=http://localhost:3000/ \
  -e GITEA__service__DISABLE_REGISTRATION=true -e GITEA__log__LEVEL=Warn docker.io/gitea/gitea:28.1.0-rootless >/dev/null
PORT=$("$ENGINE" port "$C" 3000/tcp | head -1 | sed 's/.*://')
for i in $(seq 1 60); do curl -fsS "http://127.0.0.1:$PORT/api/healthz" >/dev/null 2>&1 && break; sleep 1; done
PW=$(head -c 18 /dev/urandom | base64 | tr -dc A-Za-z0-9 | head -c 20)
"$ENGINE" exec "$C" gitea admin user create --admin --username replicant-bot --password "$PW" --email bot@replicant.localhost \
  --must-change-password=false >/dev/null
TOK=$(curl -fsS -u "replicant-bot:$PW" -H 'Content-Type: application/json' -X POST "http://127.0.0.1:$PORT/api/v1/users/replicant-bot/tokens" \
  -d '{"name":"t","scopes":["write:repository","write:issue","write:organization","write:user"]}' | python3 -c 'import sys,json;print(json.load(sys.stdin)["sha1"])')
curl -fsS -H "Authorization: token $TOK" -H 'Content-Type: application/json' -X POST "http://127.0.0.1:$PORT/api/v1/orgs" \
  -d '{"username":"acme-payments"}' >/dev/null
curl -fsS -H "Authorization: token $TOK" -H 'Content-Type: application/json' -X POST "http://127.0.0.1:$PORT/api/v1/orgs/acme-payments/repos" \
  -d '{"name":"ledger-api","default_branch":"main","auto_init":true,"readme":"Default"}' >/dev/null
echo "$PORT $TOK"
