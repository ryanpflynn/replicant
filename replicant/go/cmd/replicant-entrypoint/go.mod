module example.com/replicant/cmd/replicant-entrypoint

go 1.26.0

require (
	example.com/replicant/internal/runner v0.0.0
	github.com/klauspost/compress v1.20.3
)

require (
	github.com/aws/aws-sdk-go-v2 v1.47.4 // indirect
	github.com/aws/aws-sdk-go-v2/aws/protocol/eventstream v1.7.24 // indirect
	github.com/aws/aws-sdk-go-v2/credentials v1.20.10 // indirect
	github.com/aws/aws-sdk-go-v2/internal/configsources v1.5.7 // indirect
	github.com/aws/aws-sdk-go-v2/internal/endpoints/v2 v2.8.7 // indirect
	github.com/aws/aws-sdk-go-v2/internal/v4a v1.5.7 // indirect
	github.com/aws/aws-sdk-go-v2/service/internal/accept-encoding v1.13.22 // indirect
	github.com/aws/aws-sdk-go-v2/service/internal/checksum v1.11.8 // indirect
	github.com/aws/aws-sdk-go-v2/service/internal/presigned-url v1.14.7 // indirect
	github.com/aws/aws-sdk-go-v2/service/internal/s3shared v1.20.7 // indirect
	github.com/aws/aws-sdk-go-v2/service/s3 v1.114.5 // indirect
	github.com/aws/smithy-go v1.28.6 // indirect
	github.com/dlclark/regexp2 v1.12.0 // indirect
	github.com/santhosh-tekuri/jsonschema/v6 v6.0.3 // indirect
	go.yaml.in/yaml/v2 v2.4.4 // indirect
	golang.org/x/text v0.40.0 // indirect
	sigs.k8s.io/yaml v1.6.0 // indirect
)

replace example.com/replicant/internal/runner => ../../internal/runner
