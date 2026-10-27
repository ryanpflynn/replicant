# sandbox-sidecars

WS-02 local sidecar image (`localhost/replicant/sandbox-sidecars:dev`): `uploader` (ships spool chunks ≤ 10 s, QA
checkpoints, then the outputs index entries with C-ART tags and `manifest.json` last with `If-None-Match: *`;
exit 0/10/20), `watchdog` (workspace usage → `/run/replicant/ipc/status/disk-*`), `workspace-guard`, local `volume-init`.
`--target pause` builds `localhost/replicant/pause:3.10` (the pod group's network namespace holder).

- Build: `podman build -t localhost/replicant/sandbox-sidecars:dev --target sidecars -f go/cmd/sandbox-sidecars/Dockerfile .`
  and `--target pause -t localhost/replicant/pause:3.10`.
