# Aliases for the replicant CLI (uv run replicant dev ...). ARGS passes options, e.g. make test ARGS="--only go,web".
.PHONY: test test-e2e prune

test:
	uv run replicant dev test unit $(ARGS)

test-e2e:
	uv run replicant dev test e2e $(ARGS)

prune:
	uv run replicant dev prune $(ARGS)
