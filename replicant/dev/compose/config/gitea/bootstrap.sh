#!/bin/sh
# Creates the Gitea site admin and the replicant-bot service user. Idempotent. Everything else
# (users alice..erin, org acme-payments, repos, org webhook, branch protection) is created through the
# Gitea API by `replicant dev seed`, so it can be replayed and reset without touching this container.
set -eu
CONF=/etc/gitea/app.ini
ADMIN_PW="$(cat "$GITEA_ADMIN_PASSWORD_FILE")"
BOT_PW="$(cat "$GITEA_BOT_PASSWORD_FILE")"
gitea admin user list --config "$CONF" | awk '{print $2}' | grep -qx gitea-admin || \
  gitea admin user create --config "$CONF" --admin --username gitea-admin --password "$ADMIN_PW" \
    --email gitea-admin@acme.test --must-change-password=false
gitea admin user list --config "$CONF" | awk '{print $2}' | grep -qx replicant-bot || \
  gitea admin user create --config "$CONF" --username replicant-bot --password "$BOT_PW" \
    --email replicant-bot@acme.test --must-change-password=false
echo "gitea users ready"
