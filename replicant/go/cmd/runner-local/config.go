package main

import (
	"os"
	"regexp"
	"strings"
	"time"

	"sigs.k8s.io/yaml"

	"example.com/replicant/internal/runner/httpx"
)

// Config is runner-local's effective configuration: the WS-11 runner-local.yaml (keys read leniently,
// `${VAR}` / `${VAR:-def}` expanded from the environment) overridden by the RUNNER_* environment of compose.yaml.
type Config struct {
	OrchestrationListen string
	SandboxListen       string
	Issuer              string
	SigningKeyFile      string
	SigningKid          string
	WorkerTokensFile    string
	ContainerHost       string
	Namespace           string // Temporal namespace / callback namespace (replicant-local)
	Env                 string
	Org                 string
	StateDir            string
	StateVolume         string
	StateHostDir        string // when runner-local runs on the host: bind-mount source for state subpaths

	SandboxNetwork string
	SandboxDNS     []string
	SandboxEnv     map[string]string
	Runtime        string
	MaxConcurrent  int
	MaxCPUs        float64
	MaxMemoryBytes int64

	TemporalAddress   string
	TemporalNamespace string
	CodecKeyFile      string

	GrantsIssuer        string
	GrantsJWKSURL       string
	GrantsRevocationURL string
	GrantsPoll          time.Duration

	RevocationsBucket      string
	RevocationsPrefix      string
	RevocationsReadersFile string
	RevocationsRetain      time.Duration

	S3Endpoint        string
	S3PublicEndpoint  string // endpoint sandboxes use
	S3STSEndpoint     string
	S3Region          string
	S3AccessKey       string
	S3SecretFile      string
	S3CredentialsFile string // INI (replicant-services) – preferred in M1 (RUNNER-05)
	SandboxCredsFile  string // INI used for mode static
	CredentialsMode   string // auto | sts | per-run-identity | static
	S3SendChecksum    bool
	RegistryBucket    string

	KillSwitchMirror string

	ImageRegistry   string
	ImageTag        string
	PauseImage      string
	SidecarsImage   string
	PullPolicy      string
	StopGraceExtra  int
	ContainerTTL    time.Duration
	RecordRetention time.Duration
	LeaseSeconds    int

	// Sandbox-facing endpoints (bootstrap.json); M1 has no sbx-edge.
	EPRunnerSandbox string
	EPAIGateway     string
	EPMemoryMCP     string
	EPContextMCP    string
	EPRepoMirror    string
	EPOTLP          string

	ReadyRequireTemporal bool
}

var varRe = regexp.MustCompile(`\$\{([A-Za-z_][A-Za-z0-9_]*)(:-([^}]*))?\}`)

// expandDefaults are the compose defaults of variables runner-local.yaml references.
var expandDefaults = map[string]string{"REPLICANT_IMAGE_REGISTRY": "localhost/replicant", "RUNNER_LOCAL_RUNTIME": "runc",
	"RUNNER_STATE_DIR": "/var/lib/runner-local", "RUNNER_MAX_CONCURRENT_RUNS": "2"}

func expand(s string) string {
	return varRe.ReplaceAllStringFunc(s, func(m string) string {
		g := varRe.FindStringSubmatch(m)
		if v := os.Getenv(g[1]); v != "" {
			return v
		}
		if g[3] == "" {
			if d, ok := expandDefaults[g[1]]; ok {
				return d
			}
		}
		return g[3]
	})
}

func loadConfig() (*Config, error) {
	var file map[string]any
	if p := httpx.Env("RUNNER_CONFIG", ""); p != "" {
		b, err := os.ReadFile(p)
		if err == nil {
			_ = yaml.Unmarshal([]byte(expand(string(b))), &file)
		}
	}
	get := func(path string) string {
		var cur any = file
		for _, k := range strings.Split(path, ".") {
			m, ok := cur.(map[string]any)
			if !ok {
				return ""
			}
			cur = m[k]
		}
		switch v := cur.(type) {
		case string:
			return v
		case bool:
			if v {
				return "true"
			}
			return "false"
		case float64:
			return strings.TrimSuffix(strings.TrimRight(strings.TrimRight(fmtFloat(v), "0"), "."), ".")
		}
		return ""
	}
	or := func(env, filePath, def string) string {
		if v := os.Getenv(env); v != "" {
			return v
		}
		if filePath != "" {
			if v := get(filePath); v != "" {
				return v
			}
		}
		return def
	}
	c := &Config{
		OrchestrationListen:    or("RUNNER_ORCHESTRATION_LISTEN", "listeners.orchestration.addr", "0.0.0.0:8480"),
		SandboxListen:          or("RUNNER_SANDBOX_LISTEN", "listeners.sandbox.addr", "0.0.0.0:8490"),
		Issuer:                 or("RUNNER_ISSUER", "", "http://runner-local:8480"),
		SigningKeyFile:         or("RUNNER_SIGNING_KEY_FILE", "", "/run/secrets/run_token_signing_key"),
		SigningKid:             or("RUNNER_SIGNING_KID", "", ""),
		WorkerTokensFile:       or("RUNNER_WORKER_TOKENS_FILE", "", "/run/secrets/runner_worker_tokens"),
		ContainerHost:          or("RUNNER_CONTAINER_HOST", "container_host", "unix:///var/run/docker.sock"),
		Namespace:              or("RUNNER_TEMPORAL_NAMESPACE", "notifier.namespace", "replicant-local"),
		Env:                    or("REPLICANT_ENV", "artifacts.env", "local"),
		Org:                    or("REPLICANT_ORG", "artifacts.org", "local"),
		StateDir:               or("RUNNER_STATE_DIR", "state_dir", "/var/lib/runner-local"),
		StateVolume:            or("RUNNER_STATE_VOLUME", "", ""),
		StateHostDir:           or("RUNNER_STATE_HOST_DIR", "", ""),
		SandboxNetwork:         or("RUNNER_SANDBOX_NETWORK", "sandbox.network", "replicant-sbx"),
		SandboxDNS:             httpx.List(or("RUNNER_SANDBOX_DNS", "", "")),
		Runtime:                or("RUNNER_LOCAL_RUNTIME", "sandbox.runtime.default", "runc"),
		MaxConcurrent:          atoiDef(or("RUNNER_MAX_CONCURRENT_RUNS", "max_concurrent_runs", "2"), 2),
		MaxCPUs:                atofDef(or("RUNNER_MAX_SANDBOX_CPUS", "", "4"), 4),
		MaxMemoryBytes:         parseBytes(or("RUNNER_MAX_SANDBOX_MEMORY", "", "4Gi")),
		TemporalAddress:        or("RUNNER_TEMPORAL_ADDRESS", "notifier.temporal_address", ""),
		TemporalNamespace:      or("RUNNER_TEMPORAL_NAMESPACE", "notifier.namespace", "replicant-local"),
		CodecKeyFile:           or("RUNNER_TEMPORAL_CODEC_KEY_FILE", "", "/run/secrets/temporal_codec_key"),
		GrantsIssuer:           or("RUNNER_GRANTS_ISSUER", "grants.issuer", "replicant-local-core"),
		GrantsJWKSURL:          or("RUNNER_GRANTS_JWKS_URL", "grants.jwks_url", "http://grants.replicant.localhost:8403/.well-known/jwks.json"),
		GrantsRevocationURL:    or("RUNNER_GRANTS_REVOCATION_URL", "grants.revocation_url", "http://grants.replicant.localhost:8403/grants/revoked"),
		GrantsPoll:             time.Duration(atoiDef(or("RUNNER_GRANTS_REVOCATION_POLL_SECONDS", "grants.revocation_poll_seconds", "60"), 60)) * time.Second,
		RevocationsBucket:      or("RUNNER_REVOCATIONS_BUCKET", "revocations.bucket", "local-replicant-local-ops"),
		RevocationsPrefix:      or("RUNNER_REVOCATIONS_PREFIX", "revocations.prefix", "exec-revocations/local/"),
		RevocationsReadersFile: or("RUNNER_REVOCATIONS_READERS_FILE", "revocations.readers_file", "/run/secrets/runner_revocations_readers"),
		RevocationsRetain:      time.Duration(atoiDef(or("RUNNER_REVOCATIONS_RETAIN_SECONDS", "revocations.retain_seconds", "7200"), 7200)) * time.Second,
		S3Endpoint:             or("RUNNER_S3_ENDPOINT", "", "http://s3.replicant.localhost:8333"),
		S3PublicEndpoint:       or("RUNNER_SANDBOX_S3_ENDPOINT", "", ""),
		S3STSEndpoint:          or("RUNNER_S3_STS_ENDPOINT", "", ""),
		S3Region:               or("REPLICANT_S3_REGION", "", "us-east-1"),
		S3AccessKey:            or("RUNNER_S3_ACCESS_KEY_ID", "", "runner-local"),
		S3SecretFile:           or("RUNNER_S3_SECRET_ACCESS_KEY_FILE", "", "/run/secrets/s3_runner_secret"),
		S3CredentialsFile:      or("RUNNER_S3_CREDENTIALS_FILE", "", ""),
		SandboxCredsFile:       or("RUNNER_SANDBOX_S3_CREDENTIALS_FILE", "", ""),
		CredentialsMode:        or("RUNNER_CREDENTIALS_MODE", "credentials.mode", "auto"),
		S3SendChecksum:         httpx.EnvBool("RUNNER_S3_SEND_CHECKSUM", false),
		RegistryBucket:         or("RUNNER_REGISTRY_BUCKET", "", "local-replicant-local-registry"),
		KillSwitchMirror:       or("REPLICANT_KILL_SWITCH_MIRROR", "", ""),
		ImageRegistry:          or("REPLICANT_IMAGE_REGISTRY", "", "localhost/replicant"),
		ImageTag:               or("REPLICANT_IMAGE_TAG", "", "dev"),
		PullPolicy:             or("RUNNER_PULL_POLICY", "executor.pull_policy", "if-not-present"),
		StopGraceExtra:         atoiDef(or("RUNNER_STOP_GRACE_EXTRA_SECONDS", "executor.stop_grace_seconds_extra", "15"), 15),
		ContainerTTL:           parseDur(or("RUNNER_CONTAINER_TTL", "gc.container_ttl_after_terminal", "1h"), time.Hour),
		RecordRetention:        parseDur(or("RUNNER_AGENTRUN_RETENTION", "gc.agentrun_retention", "1080h"), 1080*time.Hour),
		LeaseSeconds:           atoiDef(or("RUNNER_LEASE_SECONDS", "lease.lease_seconds", "120"), 120),
		EPRunnerSandbox:        or("RUNNER_SANDBOX_RUNNER_URL", "", "http://runner-local:8490"),
		EPAIGateway:            or("RUNNER_SANDBOX_GATEWAY_URL", "", "http://gateway-shim:8420"),
		EPMemoryMCP:            or("RUNNER_SANDBOX_MEMORY_MCP_URL", "", ""),  // M1: no memory-service
		EPContextMCP:           or("RUNNER_SANDBOX_CONTEXT_MCP_URL", "", ""), // M1: no context-mcp
		EPRepoMirror:           or("RUNNER_SANDBOX_REPO_MIRROR_URL", "", "http://repo-mirror:8080"),
		EPOTLP:                 or("RUNNER_SANDBOX_OTLP_URL", "", ""),
		ReadyRequireTemporal:   httpx.EnvBool("RUNNER_READY_REQUIRE_TEMPORAL", true),
	}
	c.PauseImage = or("RUNNER_PAUSE_IMAGE", "sandbox.pod_group.pause_image", c.ImageRegistry+"/pause:3.10")
	c.SidecarsImage = or("RUNNER_SIDECARS_IMAGE", "", c.ImageRegistry+"/sandbox-sidecars:"+c.ImageTag)
	if c.S3PublicEndpoint == "" {
		c.S3PublicEndpoint = c.S3Endpoint
	}
	if c.S3STSEndpoint == "" {
		c.S3STSEndpoint = c.S3Endpoint
	}
	c.SandboxEnv = map[string]string{}
	if m, ok := file["sandbox"].(map[string]any); ok {
		if e, ok := m["env"].(map[string]any); ok {
			for k, v := range e {
				if s, ok := v.(string); ok {
					c.SandboxEnv[k] = s
				}
			}
		}
	}
	// M1: no egress proxy or package mirrors (PLAN "sandbox egress is allowed directly"); allow switching the
	// file's proxy/mirror env off without editing the WS-11 file.
	// Default: direct egress whenever sandboxes are not on the internal replicant-sbx network (v0: replicant-core,
	// where egress.sbx.local and the mirrors do not exist).
	if httpx.EnvBool("RUNNER_SANDBOX_DIRECT_EGRESS", c.SandboxNetwork != "replicant-sbx") {
		for _, k := range []string{"HTTPS_PROXY", "HTTP_PROXY", "NPM_CONFIG_REGISTRY", "GOPROXY", "PIP_INDEX_URL", "OTEL_EXPORTER_OTLP_ENDPOINT", "OTEL_EXPORTER_OTLP_PROTOCOL"} {
			delete(c.SandboxEnv, k)
		}
	}
	if v := os.Getenv("RUNNER_SANDBOX_EXTRA_ENV"); v != "" {
		for _, kv := range httpx.List(v) {
			if k, val, ok := strings.Cut(kv, "="); ok {
				c.SandboxEnv[k] = val
			}
		}
	}
	return c, nil
}
