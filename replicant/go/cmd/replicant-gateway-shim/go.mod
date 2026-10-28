module example.com/replicant/cmd/replicant-gateway-shim

go 1.26.0

require (
	example.com/replicant/internal/runner v0.0.0
	github.com/klauspost/compress v1.20.3
	gopkg.in/yaml.v3 v3.0.1
)

replace example.com/replicant/internal/runner => ../../internal/runner
