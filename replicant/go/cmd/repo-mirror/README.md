# repo-mirror

Read-only git smart HTTP (`git http-backend`, upload-pack only, protocol v2, `allowFilter`, `allowAnySHA1InWant`) for
sandboxes at `/git/<repo_id>/…`, authenticated by the `repo-mirror` run token (Basic password, injected by agentd)
with all C-RUNNER verifier obligations (runner JWKS, grant, core revocation list, exec feed). Upstream fetches use a
github-token-broker token (Gitea in M1); the upstream `owner/name` (agentd headers) is checked against the Gitea API id.

- Build: `podman build -t localhost/replicant/repo-mirror:dev -f go/cmd/repo-mirror/Dockerfile .`
- Test: `podman run --rm -v "$PWD":/src -w /src/go/cmd/repo-mirror -e GOWORK=off docker.io/library/golang:1.26 go test ./...`
- Env: the spec `MIRROR_*` set (compose.yaml); healthcheck `repo-mirror healthcheck --url http://127.0.0.1:8080/readyz`.
