# sandbox-sidecars

WS-02 local sidecar image (`localhost/replicant/sandbox-sidecars:dev`): `uploader` (ships spool chunks ≤ 10 s, QA
checkpoints, then the outputs index entries with C-ART tags and `manifest.json` last with `If-None-Match: *`;
exit 0/10/20; also runs the disk watchdog goroutine: workspace usage → `/run/replicant/ipc/status/disk-*`),
local `sandbox-init` (volume ownership, then the workspace-guard check, exit 1/84), and `pause` (the pod group's
network namespace holder; runner-local starts it from this image unless `RUNNER_PAUSE_IMAGE` names another).

- Build: `podman build -t localhost/replicant/sandbox-sidecars:dev --target sidecars -f go/cmd/sandbox-sidecars/Dockerfile .`
