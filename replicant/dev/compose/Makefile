# Thin wrapper; the real logic lives in the `replicant dev` CLI (stack.md §4). Kept so `make up` works on a
# fresh clone before the CLI is installed (it installs it with uv).
SHELL := /bin/bash
REPLICANT ?= uv run --project ../../cli replicant
.PHONY: up up-obs down seed reset status doctor k8s logs build test-contract-runner
up:        ; $(REPLICANT) dev up
up-obs:    ; $(REPLICANT) dev up --with-observability
down:      ; $(REPLICANT) dev down
seed:      ; $(REPLICANT) dev seed
reset:     ; $(REPLICANT) dev reset --yes
status:    ; $(REPLICANT) dev status
doctor:    ; $(REPLICANT) dev doctor
k8s:       ; $(REPLICANT) dev up --with-k8s
logs:      ; $(REPLICANT) dev logs --follow
build:     ; $(REPLICANT) dev build
test-contract-runner: ; $(REPLICANT) dev test contract-runner --runner local && $(REPLICANT) dev test contract-runner --runner k8s
