#!/bin/bash
# SEC-EGRESS-01..07 (spec/11-local-dev/test-strategy.md §8.1), executed INSIDE a runner-local sandbox: the agent
# container of a real run (the gateway script `runner-contract-egress/egress_probe` makes Claude Code run this file
# through its Bash tool). Output: one line per check, `SEC-EGRESS-0N PASS|FAIL <detail>`, written to the run's
# handoff output and read back by the runner contract driver (go/cmd/runner-local/smoke, case SEC-EGRESS).
# @CORE_TARGETS@ is replaced by tests/runner-contract/run-v0.sh with "name=ip:port ..." of core services.
CORE_TARGETS="@CORE_TARGETS@"
out() { echo "$1 $2 $3"; }
tcp() { timeout "${3:-4}" bash -c "exec 3<>/dev/tcp/$1/$2" 2>/dev/null; }
resolves() { getent hosts "$1" >/dev/null 2>&1; }
code() { curl -s -o /dev/null -w '%{http_code}' --max-time "${2:-8}" "$1" 2>/dev/null; }

# 01: direct HTTPS and via the egress proxy
d=""; resolves example.com && d="resolves" || d="NXDOMAIN"
direct=$(curl -s -o /dev/null -w '%{http_code}' --noproxy '*' --max-time 6 https://example.com 2>/dev/null)
viaproxy=$(curl -s -o /dev/null -w '%{http_connect}' --max-time 8 -x "${HTTPS_PROXY:-http://egress.sbx.local:3128}" https://example.com 2>/dev/null)
[ "$d" = NXDOMAIN ] && [ "$direct" = 000 ] && [ "$viaproxy" = 403 ] && r=PASS || r=FAIL
out SEC-EGRESS-01 $r "dns=$d direct=$direct proxy_connect=$viaproxy proxy=${HTTPS_PROXY:-unset}"

# 02: instance metadata
m=$(code http://169.254.169.254/latest/meta-data/ 5); tcp 169.254.169.254 80 3 && t=open || t=no_route
[ "$m" = 000 ] && [ "$t" = no_route ] && r=PASS || r=FAIL
out SEC-EGRESS-02 $r "http=$m tcp=$t"

# 03: core services by name (NXDOMAIN) and by IP (no route)
r=PASS; det=""
for n in runner-local postgres-replicant temporal github-token-broker control-plane-api; do
  resolves "$n" && { r=FAIL; det="$det $n:resolves"; } || det="$det $n:NXDOMAIN"
done
for t in $CORE_TARGETS; do
  name=${t%%=*}; hp=${t#*=}; ip=${hp%:*}; port=${hp#*:}
  tcp "$ip" "$port" 3 && { r=FAIL; det="$det $name@$hp:open"; } || det="$det $name@$hp:no_route"
done
[ -n "$CORE_TARGETS" ] || { r=FAIL; det="$det (no core targets given)"; }
out SEC-EGRESS-03 $r "$det"

# 04: push through the sandbox's git path (agentd -> git.sbx.local -> repo-mirror) and directly to git.sbx.local
cd "${REPLICANT_WORKSPACE_DIR:-/workspace}/repo" 2>/dev/null || cd /workspace/repo 2>/dev/null
push=$(git -c user.name=p -c user.email=p@x push origin HEAD:refs/heads/egress-probe-$$ 2>&1 | tail -1 | tr -s ' ' | cut -c1-120); pushrc=$?
git ls-remote origin "refs/heads/egress-probe-$$" 2>/dev/null | grep -q egress-probe && pushed=yes || pushed=no
rp=$(curl -s -o /dev/null -w '%{http_code}' --max-time 6 -X POST -H 'Content-Type: application/x-git-receive-pack-request' http://git.sbx.local/acme-payments/ledger-api.git/git-receive-pack)
rpinfo=$(curl -s -o /dev/null -w '%{http_code}' --max-time 6 "http://git.sbx.local/acme-payments/ledger-api.git/info/refs?service=git-receive-pack")
# a credential helper set to "" (the entrypoint disables helpers that way) is not a credential
creds=$( (env | grep -E -i '^(GITHUB_TOKEN|GITEA_TOKEN|GIT_ASKPASS|GH_TOKEN)=' | cut -d= -f1; git config --get-regexp 'credential|extraheader' 2>/dev/null | awk 'NF>1 {print $1}') | tr '\n' ' ')
[ "$pushed" = no ] && [ "$rp" = 403 ] && { [ "$rpinfo" = 403 ] || [ "$rpinfo" = 401 ] || [ "$rpinfo" = 404 ]; } && [ -z "$creds" ] && r=PASS || r=FAIL
out SEC-EGRESS-04 $r "pushed=$pushed push='$push' receive-pack=$rp info/refs?receive-pack=$rpinfo credentials='${creds}'"

# 05: DNS tunnelling
resolves x.attacker.example.net && a=resolves || a=NXDOMAIN
tcp 8.8.8.8 53 3 && g=open || g=no_route
tcp 1.1.1.1 53 3 && c=open || c=no_route
[ "$a" = NXDOMAIN ] && [ "$g" = no_route ] && [ "$c" = no_route ] && r=PASS || r=FAIL
out SEC-EGRESS-05 $r "x.attacker.example.net=$a 8.8.8.8:53=$g 1.1.1.1:53=$c"

# 06: the allowed path: go get through the egress proxy to the allowlisted Go proxy (v0 has no package mirrors;
# npm/PyPI mirrors exist only in the core profile, PLATFORM-D18)
tmp=$(mktemp -d); ( cd "$tmp" && go mod init probe.local/x >/dev/null 2>&1 && \
  GOFLAGS=-mod=mod GOPATH="$tmp/gopath" GOMODCACHE="$tmp/gopath/mod" GOCACHE="$tmp/gocache" timeout 120 go get github.com/google/uuid@v1.6.0 >"$tmp/go.log" 2>&1 ); goget=$?
other=$(curl -s -o /dev/null -w '%{http_connect}' --max-time 8 -x "${HTTPS_PROXY:-http://egress.sbx.local:3128}" https://registry.npmjs.org/left-pad 2>/dev/null)
[ "$goget" = 0 ] && [ "$other" = 403 ] && r=PASS || r=FAIL
out SEC-EGRESS-06 $r "go_get_rc=$goget goproxy=${GOPROXY:-unset} via ${HTTPS_PROXY:-no proxy}; non-allowlisted registry via proxy=$other $( [ "$goget" = 0 ] || tail -2 "$tmp/go.log" | tr '\n' ' ' | cut -c1-200)"

# 07: no container-engine socket, no Kubernetes service account, no token directories in the agent
f=""
for p in /var/run/docker.sock /run/docker.sock /run/podman/podman.sock /var/run/secrets/kubernetes.io/serviceaccount/token /run/replicant/tokens /run/replicant/token-seed; do
  [ -e "$p" ] && f="$f $p"
done
envbad=$(env | cut -d= -f1 | grep -E '^(REPLICANT_RUN_TOKEN_DIR|REPLICANT_RUN_TOKEN_SEED_DIR|KUBERNETES_SERVICE_HOST|DOCKER_HOST|AWS_SECRET_ACCESS_KEY|AWS_SESSION_TOKEN)$' | tr '\n' ' ')
[ -z "$f" ] && [ -z "$envbad" ] && r=PASS || r=FAIL
out SEC-EGRESS-07 $r "present='${f}' env='${envbad}'"
rm -rf "$tmp"
