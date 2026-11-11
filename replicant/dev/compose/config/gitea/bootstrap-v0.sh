#!/bin/sh
# v0 gitea-bootstrap (PLATFORM-D6). Idempotent. Never prints a secret.
#  1. the spec bootstrap.sh: site admin `gitea-admin` and the `replicant-bot` user (CLI)
#  2. human user `alice` (random password; the E2E driver acts as her through the admin `Sudo` header)
#  3. org acme-payments, team payments-maintainers (write) with replicant-bot and alice
#  4. repo acme-payments/<name> for each GITEA_SAMPLE_REPOS entry plus billing-py and web-console, initial commit
#     pushed as replicant-bot
#  5. repo label `replicant`
#  6. org webhook -> github-integration /webhooks/gitea (type gitea, json, secret = gitea_webhook_secret)
set -eu
/bin/sh /scripts/bootstrap.sh
CONF=/etc/gitea/app.ini
G="${GITEA_URL:?}"; ORG="${GITEA_ORG:-acme-payments}"
ADMIN_PW="$(cat "$GITEA_ADMIN_PASSWORD_FILE")"; BOT_PW="$(cat "$GITEA_BOT_PASSWORD_FILE")"
HOOK_SECRET="$(cat "$GITEA_WEBHOOK_SECRET_FILE")"
NETRC=/tmp/.netrc-admin
umask 077
printf 'machine %s login gitea-admin password %s\n' "$(echo "$G" | sed -E 's#^https?://([^:/]+).*#\1#')" "$ADMIN_PW" > "$NETRC"
api() { # api METHOD PATH [JSON] -> body on stdout, HTTP code in $CODE_FILE
  m="$1"; p="$2"; d="${3:-}"
  if [ -n "$d" ]; then
    curl -sS --netrc-file "$NETRC" -o /tmp/api.out -w '%{http_code}' -X "$m" -H 'Content-Type: application/json' --data "$d" "$G/api/v1$p" > /tmp/api.code
  else
    curl -sS --netrc-file "$NETRC" -o /tmp/api.out -w '%{http_code}' -X "$m" "$G/api/v1$p" > /tmp/api.code
  fi
  cat /tmp/api.out
}
code() { cat /tmp/api.code; }
need() { c="$(code)"; case "$c" in 2*) ;; *) echo "gitea API $1 failed: HTTP $c: $(head -c 300 /tmp/api.out)"; exit 1;; esac; }

i=0; until curl -fsS -o /dev/null "$G/api/healthz"; do i=$((i+1)); [ "$i" -gt 60 ] && { echo "gitea not reachable"; exit 1; }; sleep 2; done

gitea admin user list --config "$CONF" | awk '{print $2}' | grep -qx alice || \
  gitea admin user create --config "$CONF" --username alice --random-password --email alice@acme.test \
    --must-change-password=false >/dev/null
echo "user alice ready"

api GET "/orgs/$ORG" >/dev/null
if [ "$(code)" = 404 ]; then
  api POST /orgs "{\"username\":\"$ORG\",\"full_name\":\"Acme Payments\",\"visibility\":\"private\"}" >/dev/null; need "create org"
fi
echo "org $ORG ready"

team_id="$(api GET "/orgs/$ORG/teams/search?q=payments-maintainers" | sed -nE 's/.*"id":([0-9]+),"name":"payments-maintainers".*/\1/p' | head -1)"
if [ -z "$team_id" ]; then
  team_id="$(api POST "/orgs/$ORG/teams" '{"name":"payments-maintainers","permission":"write","includes_all_repositories":true,"units":["repo.code","repo.issues","repo.pulls","repo.releases","repo.wiki"]}' | sed -nE 's/^\{"id":([0-9]+).*/\1/p')"
  need "create team"
fi
for u in replicant-bot alice; do api PUT "/teams/$team_id/members/$u" >/dev/null; need "add $u to team"; done
echo "team payments-maintainers ($team_id) ready"

# Design §8c: the onboarding samples (Python, Node) are always added, whatever GITEA_SAMPLE_REPOS lists.
REPOS="${GITEA_SAMPLE_REPOS:-ledger-api}"
for extra in billing-py web-console; do case " $REPOS " in *" $extra "*) ;; *) REPOS="$REPOS $extra";; esac; done
for name in $REPOS; do
  api GET "/repos/$ORG/$name" >/dev/null
  if [ "$(code)" = 404 ]; then
    api POST "/orgs/$ORG/repos" "{\"name\":\"$name\",\"private\":true,\"default_branch\":\"main\",\"auto_init\":false}" >/dev/null; need "create repo $name"
  fi
  if api GET "/repos/$ORG/$name/branches/main" >/dev/null && [ "$(code)" = 200 ]; then
    echo "repo $ORG/$name already has main"
  else
    W="$(mktemp -d /tmp/repo.XXXXXX)"; cp -R "/sample-repos/$name/." "$W/"; rm -rf "$W/ISSUES"
    cd "$W"
    git init -q -b main
    git -c user.name=replicant-bot -c user.email=replicant-bot@acme.test add -A
    git -c user.name=replicant-bot -c user.email=replicant-bot@acme.test commit -q -m "Initial import of $name"
    # Credentials come from a helper reading the secret file, so the password is never in a URL or argv.
    git -c credential.helper= -c "credential.helper=!f() { echo username=replicant-bot; printf 'password=%s\n' \"\$(cat $GITEA_BOT_PASSWORD_FILE)\"; }; f" \
      push -q "$G/$ORG/$name.git" main
    cd /; rm -rf "$W"
    echo "repo $ORG/$name pushed"
  fi
  api GET "/repos/$ORG/$name/labels?limit=50" | grep -q '"name":"replicant"' || {
    api POST "/repos/$ORG/$name/labels" '{"name":"replicant","color":"#5319e7","description":"Hand this issue to the replicant"}' >/dev/null; need "create label"; }
  echo "label replicant on $ORG/$name ready"
done

HOOK_URL="${GITEA_WEBHOOK_URL:?}"
if api GET "/orgs/$ORG/hooks" | grep -q "\"url\":\"$HOOK_URL\""; then
  echo "org webhook already present"
else
  # Events: spec stack.md §5 step 4, plus issue_label (Gitea delivers label changes only on that event).
  api POST "/orgs/$ORG/hooks" "{\"type\":\"gitea\",\"active\":true,\"branch_filter\":\"*\",\"events\":[\"issues\",\"issue_label\",\"issue_comment\",\"pull_request\",\"pull_request_review_approved\",\"pull_request_review_rejected\",\"pull_request_review_comment\",\"pull_request_comment\",\"pull_request_sync\"],\"config\":{\"url\":\"$HOOK_URL\",\"content_type\":\"json\",\"secret\":\"$HOOK_SECRET\"}}" >/dev/null
  need "create org webhook"
  echo "org webhook -> $HOOK_URL created"
fi
rm -f "$NETRC" /tmp/api.out /tmp/api.code
echo "gitea v0 bootstrap done"
