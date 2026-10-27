module example.com/replicant/cmd/replicant-qa

go 1.26.0

require example.com/replicant/internal/runner v0.0.0

require (
	github.com/dlclark/regexp2 v1.12.0 // indirect
	github.com/santhosh-tekuri/jsonschema/v6 v6.0.3 // indirect
	go.yaml.in/yaml/v2 v2.4.4 // indirect
	golang.org/x/text v0.40.0 // indirect
	sigs.k8s.io/yaml v1.6.0 // indirect
)

replace example.com/replicant/internal/runner => ../../internal/runner
