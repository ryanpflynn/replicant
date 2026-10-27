# cli-claude-code

`localhost/replicant/cli-claude-code:dev`: Claude Code 2.1.310 (npm `@anthropic-ai/claude-code-linux-<arch>`, sha512
checked) at `/opt/replicant/cli/claude-code/2.1.310/claude`, plus `/opt/replicant/bin/{replicant-entrypoint,replicant-agentd,
replicant-qa}` and `/opt/replicant/etc/gitconfig`. Used for `replicant-agentd` and `prepare`; `prepare` copies `/opt/replicant`
into the run's `tools` volume, which the environment image mounts read-only.

`podman build -t localhost/replicant/cli-claude-code:dev -f images/cli-claude-code/Dockerfile .` (repo root).
