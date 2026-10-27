#!/bin/bash
# RC-ENV-01..04 and RC-RUN-08 evidence, printed as KEY=VALUE lines into the run's handoff.
echo "ENV_NAMES=$(env | cut -d= -f1 | sort | tr '\n' ',')"
for d in /run/replicant/token-seed /run/replicant/tokens /var/run/secrets/kubernetes.io; do [ -e "$d" ] && echo "PATH_PRESENT=$d" || echo "PATH_ABSENT=$d"; done
for v in HTTPS_PROXY HTTP_PROXY NO_PROXY NPM_CONFIG_REGISTRY GOPROXY PIP_INDEX_URL; do echo "$v=${!v}"; done
# the sandbox environment as the runner set it: PID 1 (replicant-entrypoint) of the agent container; the CLI's tool
# processes see their own TRACEPARENT (the CLI's span)
for v in TRACEPARENT OTEL_RESOURCE_ATTRIBUTES; do echo "$v=$(tr '\0' '\n' < /proc/1/environ 2>/dev/null | sed -n "s/^$v=//p")"; done
echo "ANTHROPIC_AUTH_TOKEN_SET=${ANTHROPIC_AUTH_TOKEN:+yes}"
cd "${REPLICANT_WORKSPACE_DIR:-/workspace}/repo" 2>/dev/null || cd /workspace/repo
echo "HEAD=$(git rev-parse HEAD)"
t=$(mktemp -d); cp -r . "$t/r" && cd "$t/r" && echo probe > .rc-env-04 && git add .rc-env-04 && git -c user.name=probe -c user.email=probe@x commit -q -m probe && echo "GIT_DATES=$(git log -1 --format='%aI|%cI')"; cd /; rm -rf "$t"
p=$PPID
for i in 1 2 3 4 5 6; do
  c=$(tr '\0' ' ' < /proc/$p/cmdline 2>/dev/null)
  case "$c" in *--session-id*) echo "CLI_SESSION=$(echo "$c" | sed -n 's/.*--session-id[ =]\([0-9a-f-]*\).*/\1/p')"; break;; esac
  p=$(awk '{print $4}' /proc/$p/stat 2>/dev/null); [ -n "$p" ] || break
done
echo "PROBE_DONE=1"
