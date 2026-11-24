# dev/compose

`compose.v0.yaml` is the local stack: standalone, 14 services (one of them the one-shot `init`), plus the opt-in profiles `slack`
(slack-mock; core's Slack module on) and `observability`. Images: postgres, `replicant-py` (images/replicant-py: every
Python role, chosen by `command:`), `replicant-go` (images/replicant-go: runner-local, gateway-shim),
control-plane-web. Use `replicant dev up` (`--profile v0,slack`, `--with-observability`); it generates `.env` and
`../.secrets/`. Deviations: `docs/v0/DEVIATIONS.md` PLATFORM-D*, SIMPLIFY-D*.

The local copies of the spec stack (`compose.yaml`, `compose.chaos.yaml`) are deleted (SIMPLIFY-D1); the spec keeps its
own under `spec/11-local-dev/compose` (authoritative names, ports, env vars, secret paths).
Smoke: `replicant dev up --services init,temporal`.

## Engines: Podman or Docker

`replicant dev` runs on Podman (podman + podman-compose) or Docker (docker + docker compose). It picks whichever daemon
answers, Podman first; force one with `REPLICANT_CONTAINER_ENGINE=podman|docker` (DOCKER-D1).

Tested: Podman 6.1 (podman machine, applehv) and Docker Engine 29.5 under Colima, with every e2e suite and the browser
tests passing on both (DOCKER-D2). **Docker Desktop itself is untested.** On a Mac with Docker Desktop:

1. Install Docker Desktop and open it once to accept the license. In Settings, give it at least 8 GB memory and a 100 GB
   disk limit, and turn on **Resources → Network → Enable host networking**: the browser tests run Playwright with
   `--network host` and fail without it.
2. Leave at least 30 GB free on the Mac. Image rebuilds leave several GB behind each time, and a full host disk makes the
   VM fail writes and damage images (DOCKER-D3); run `replicant dev prune` (add `--images` for old image tags) when
   space runs low.
3. Create `replicant/dev/.env` with `ANTHROPIC_API_KEY` if you will use `--gateway passthrough` (real Claude); scripted
   runs need no key.
4. `REPLICANT_CONTAINER_ENGINE=docker uv run replicant dev up --seed`, then
   `REPLICANT_CONTAINER_ENGINE=docker uv run replicant dev test e2e` (add `--profile v0,slack` to `dev up` for e2e02/e2e03).
5. The runner talks to `/var/run/docker.sock` inside the VM, which Docker Desktop provides. If sandboxes fail to start,
   set `REPLICANT_CONTAINER_SOCKET` to the socket path inside the VM.

Docker-specific behaviour already handled: plain run volumes get no driver options, the engine client negotiates API
v1.45, the sandbox `/tmp` is mounted `exec`, squid-free egress via sbx-edge, and a fixed SeaweedFS volume count
(see DOCKER-D2, SIMPLIFY-D30/D31).
