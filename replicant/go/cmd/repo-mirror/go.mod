module example.com/replicant/cmd/repo-mirror

go 1.26.0

require example.com/replicant/internal/runner v0.0.0

require (
	go.yaml.in/yaml/v2 v2.4.4 // indirect
	sigs.k8s.io/yaml v1.6.0 // indirect
)

replace example.com/replicant/internal/runner => ../../internal/runner
