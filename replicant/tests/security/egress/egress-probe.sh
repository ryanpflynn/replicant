#!/bin/bash
# SEC-EGRESS-01..07 (spec/11-local-dev/test-strategy.md §8.1) and 09..11 (SIMPLIFY-D30/D31: static sandbox hosts,
# sbx-edge SNI allowlist), executed INSIDE a runner-local sandbox: the agent
# container of a real run (the gateway script `runner-contract-egress/egress_probe` makes Claude Code run this file
# through its Bash tool). Output: one line per check, `SEC-EGRESS-0N PASS|FAIL <detail>`, written to the run's
# handoff output and read back by the runner contract driver (go/cmd/runner-local/smoke, case SEC-EGRESS).
# @CORE_TARGETS@ is replaced by tests/runner-contract/run-v0.sh with "name=ip:port ..." of core services.
CORE_TARGETS="@CORE_TARGETS@"
out() { echo "$1 $2 $3"; }
tcp() { timeout "${3:-4}" bash -c "exec 3<>/dev/tcp/$1/$2" 2>/dev/null; }
resolves() { getent hosts "$1" >/dev/null 2>&1; }
code() { curl -s -o /dev/null -w '%{http_code}' --max-time "${2:-8}" "$1" 2>/dev/null; }

# sbx-edge's address: every name the sandbox resolves maps to it (/etc/hosts of the pause container, SIMPLIFY-D30)
EDGE=$(getent hosts gateway.sbx.local 2>/dev/null | awk '{print $1; exit}')
# TLS through sbx-edge with SNI $1 (as a client that resolved $1 to sbx-edge would connect); 000 = refused
edge_tls() { curl -s -o /dev/null -w '%{http_code}' --max-time 8 --resolve "$1:443:${EDGE:-0.0.0.0}" "https://$1/" 2>/dev/null; }

# 01: direct HTTPS, and through sbx-edge with a non-allowlisted SNI; no proxy configured
d=""; resolves example.com && d="resolves" || d="unresolved"
direct=$(curl -s -o /dev/null -w '%{http_code}' --noproxy '*' --max-time 6 https://example.com 2>/dev/null)
viaedge=$(edge_tls example.com)
[ "$d" = unresolved ] && [ "$direct" = 000 ] && [ -n "$EDGE" ] && [ "$viaedge" = 000 ] && [ -z "${HTTPS_PROXY:-}${HTTP_PROXY:-}" ] && r=PASS || r=FAIL
out SEC-EGRESS-01 $r "dns=$d direct=$direct via_edge($EDGE)=$viaedge https_proxy=${HTTPS_PROXY:-unset} http_proxy=${HTTP_PROXY:-unset}"

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

# 04: the sandbox has no git endpoint (the repo arrives as a bundle in the run inputs, SIMPLIFY-D40): no remote to
# push to, git.sbx.local does not resolve, and no git credential is present
cd "${REPLICANT_WORKSPACE_DIR:-/workspace}/repo" 2>/dev/null || cd /workspace/repo 2>/dev/null
remotes=$(git remote 2>/dev/null | tr '\n' ' ')
push=$(git -c user.name=p -c user.email=p@x push origin HEAD:refs/heads/egress-probe-$$ 2>&1 | tail -1 | tr -s ' ' | cut -c1-120)
resolves git.sbx.local && gitname=resolves || gitname=unresolved
# a credential helper set to "" (the entrypoint disables helpers that way) is not a credential
creds=$( (env | grep -E -i '^(GITHUB_TOKEN|GITEA_TOKEN|GIT_ASKPASS|GH_TOKEN)=' | cut -d= -f1; git config --get-regexp 'credential|extraheader' 2>/dev/null | awk 'NF>1 {print $1}') | tr '\n' ' ')
[ -z "$remotes" ] && [ "$gitname" = unresolved ] && [ -z "$creds" ] && r=PASS || r=FAIL
out SEC-EGRESS-04 $r "remotes='${remotes}' push='$push' git.sbx.local=$gitname credentials='${creds}'"

# 05: DNS tunnelling
resolves x.attacker.example.net && a=resolves || a=NXDOMAIN
tcp 8.8.8.8 53 3 && g=open || g=no_route
tcp 1.1.1.1 53 3 && c=open || c=no_route
[ "$a" = NXDOMAIN ] && [ "$g" = no_route ] && [ "$c" = no_route ] && r=PASS || r=FAIL
out SEC-EGRESS-05 $r "x.attacker.example.net=$a 8.8.8.8:53=$g 1.1.1.1:53=$c"

# 06: the allowed path: go get straight to the allowlisted Go proxy, which resolves to sbx-edge and is passed through
# by SNI (SIMPLIFY-D31; v0 has no package mirrors, PLATFORM-D18); a non-allowlisted registry through sbx-edge is refused
tmp=$(mktemp -d); ( cd "$tmp" && go mod init probe.local/x >/dev/null 2>&1 && \
  GOFLAGS=-mod=mod GOPATH="$tmp/gopath" GOMODCACHE="$tmp/gopath/mod" GOCACHE="$tmp/gocache" timeout 120 go get github.com/google/uuid@v1.6.0 >"$tmp/go.log" 2>&1 ); goget=$?
gp=$(getent hosts proxy.golang.org 2>/dev/null | awk '{print $1; exit}')
other=$(edge_tls registry.npmjs.org)
[ "$goget" = 0 ] && [ -n "$EDGE" ] && [ "$gp" = "$EDGE" ] && [ "$other" = 000 ] && r=PASS || r=FAIL
out SEC-EGRESS-06 $r "go_get_rc=$goget goproxy=${GOPROXY:-unset} proxy.golang.org=${gp:-unresolved}; registry.npmjs.org via sbx-edge=$other $( [ "$goget" = 0 ] || tail -2 "$tmp/go.log" | tr '\n' ' ' | cut -c1-200)"

# 07: no container-engine socket, no Kubernetes service account, no token directories in the agent
f=""
for p in /var/run/docker.sock /run/docker.sock /run/podman/podman.sock /var/run/secrets/kubernetes.io/serviceaccount/token /run/replicant/tokens /run/replicant/token-seed; do
  [ -e "$p" ] && f="$f $p"
done
envbad=$(env | cut -d= -f1 | grep -E '^(REPLICANT_RUN_TOKEN_DIR|REPLICANT_RUN_TOKEN_SEED_DIR|KUBERNETES_SERVICE_HOST|DOCKER_HOST|AWS_SECRET_ACCESS_KEY|AWS_SESSION_TOKEN)$' | tr '\n' ' ')
[ -z "$f" ] && [ -z "$envbad" ] && r=PASS || r=FAIL
out SEC-EGRESS-07 $r "present='${f}' env='${envbad}'"
rm -rf "$tmp"

# every sbx.local name and allowlisted name maps to sbx-edge
r=PASS; det=""
for n in github.com registry.npmjs.org api.anthropic.com pypi.org google.com git.sbx.local; do
  resolves "$n" && { r=FAIL; det="$det $n:resolves"; } || det="$det $n:unresolved"
done
for n in runner-sandbox.sbx.local s3.sbx.local sum.golang.org; do
  a=$(getent hosts "$n" 2>/dev/null | awk '{print $1; exit}'); [ -n "$EDGE" ] && [ "$a" = "$EDGE" ] || r=FAIL; det="$det $n=${a:-unresolved}"
done
out SEC-EGRESS-09 $r "$det"

# 10: sbx-edge refuses TLS to a non-allowlisted SNI, TLS without SNI, and plain HTTP to an allowlisted name
gh=$(edge_tls github.com); an=$(edge_tls api.anthropic.com)
nosni=$(curl -sk -o /dev/null -w '%{http_code}' --max-time 8 "https://${EDGE:-0.0.0.0}/" 2>/dev/null)
plain=$(curl -s -o /dev/null -w '%{http_code}' --max-time 8 http://proxy.golang.org/ 2>/dev/null)
[ -n "$EDGE" ] && [ "$gh" = 000 ] && [ "$an" = 000 ] && [ "$nosni" = 000 ] && [ "$plain" = 000 ] && r=PASS || r=FAIL
out SEC-EGRESS-10 $r "sni_github.com=$gh sni_api.anthropic.com=$an no_sni=$nosni http_proxy.golang.org=$plain"

# 11: direct-IP egress (no name needed) has no route off the host
r=PASS; det=""
for t in 1.1.1.1:443 8.8.8.8:443 9.9.9.9:80 140.82.112.3:443; do
  tcp "${t%:*}" "${t#*:}" 4 && { r=FAIL; det="$det $t:open"; } || det="$det $t:no_route"
done
out SEC-EGRESS-11 $r "$det"
rm -rf "$tmp"
