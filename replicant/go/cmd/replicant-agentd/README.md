# replicant-agentd

WS-03 sidecar (cli-invocation.md §5, ADR-03-001): the only holder of the run tokens (seed → private tmpfs, refresh at
< 15 min), loopback proxies :7800 (gateway, `ai-gateway` token, retries 429/529/50x, usage + budget headers), :7801
(MCP), :7802 (git → repo-mirror, read-only), :7803 (inputs, closed after `prepared`), heartbeat every 30 s with the
watchdog disk report (cancel reason / revoked → IPC `cancel_requested`, 120 s without success → `heartbeat_lost`),
steering long-poll, IPC API on `/run/replicant/ipc/agentd.sock`, uploader credentials + known-credential redaction on
`/run/replicant/uploader-ipc/agentd.sock`, `POST /v1/runs/self:close` at finish. `replicant-agentd ready` is the startup probe.

- Test: `podman run --rm -v "$PWD":/src -w /src/go/cmd/replicant-agentd -e GOWORK=off docker.io/library/golang:1.26 go test ./...`
