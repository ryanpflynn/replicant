// Package sandbox holds the in-pod contracts shared by runner-local (which renders them), replicant-agentd,
// replicant-entrypoint, replicant-qa and the WS-02 sidecars: bootstrap.json (replicant.sandbox-bootstrap/v1),
// the inputs manifest, the run plan subset the entrypoint reads, the outputs index, and well-known paths.
package sandbox

import (
	"encoding/json"
	"os"
)

// Well-known paths (cli-invocation.md §1.1, §1.4; C-IDS §7–§8).
const (
	BootstrapPath      = "/etc/replicant/bootstrap.json"
	IPCDir             = "/run/replicant/ipc"
	IPCSocket          = "/run/replicant/ipc/agentd.sock"
	StatusDir          = "/run/replicant/ipc/status"
	UploaderSocket     = "/run/replicant/uploader-ipc/agentd.sock"
	TokenSeedDir       = "/run/replicant/token-seed"
	TokenDir           = "/run/replicant/tokens"
	PromptDir          = "/run/replicant/prompt"
	InputsDir          = "/replicant/inputs"
	OutDir             = "/replicant/out"
	SpoolDir           = "/replicant/spool"
	OutputsIndexPath   = "/replicant/out/outputs.json"
	ResultPath         = "/replicant/out/run_result/result.json"
	WorkspaceDir       = "/workspace"
	RepoDir            = "/workspace/repo"
	HomeDir            = "/home/agent"
	ToolsDir           = "/opt/replicant"
	ManagedClaudeDir   = "/etc/claude-code"
	TerminationDefault = "/dev/termination-log"
	LoopbackGateway    = "127.0.0.1:7800"
	LoopbackMCP        = "127.0.0.1:7801"
	LoopbackGit        = "127.0.0.1:7802"
	LoopbackInputs     = "127.0.0.1:7803"
)

// Status marker files (WS-02 watchdog and WS-03 entrypoint).
const (
	MarkerAgentStarted = StatusDir + "/agent-started"
	MarkerPrepared     = StatusDir + "/prepared"
	MarkerFinished     = StatusDir + "/finished"
	MarkerAgentExit    = StatusDir + "/agent.exit"
	MarkerDiskWarn     = StatusDir + "/disk-warn"
	MarkerDiskExceeded = StatusDir + "/disk-exceeded"
	DiskState          = StatusDir + "/disk-state"
	DiskUsed           = StatusDir + "/disk-used"
	DiskLimit          = StatusDir + "/disk-limit"
	DiskPeak           = StatusDir + "/disk-peak"
	MarkerUploaded     = StatusDir + "/manifest-written"
)

// Bootstrap is /etc/replicant/bootstrap.json.
type Bootstrap struct {
	Schema               string            `json:"schema"`
	Env                  string            `json:"env"`
	RunID                string            `json:"run_id"`
	InputsManifestURI    string            `json:"inputs_manifest_uri"`
	InputsManifestSHA256 string            `json:"inputs_manifest_sha256"`
	Endpoints            Endpoints         `json:"endpoints"`
	PackageMirrorHost    string            `json:"package_mirror_host,omitempty"`
	PackageMirrors       map[string]string `json:"package_mirrors,omitempty"`
	Repo                 BootstrapRepo     `json:"repo"`
	KMSKeyARN            string            `json:"kms_key_arn,omitempty"`
	WS02                 *WS02             `json:"ws02,omitempty"`
	// Local (runner-local) additions, ignored by production readers.
	Local *LocalBootstrap `json:"local,omitempty"`
}

type Endpoints struct {
	RunnerSandbox string `json:"runner_sandbox"`
	AIGateway     string `json:"ai_gateway"`
	MemoryMCP     string `json:"memory_mcp,omitempty"`
	ContextMCP    string `json:"context_mcp,omitempty"`
	CodeSearchMCP string `json:"code_search_mcp,omitempty"`
	RepoMirror    string `json:"repo_mirror"`
	OTLP          string `json:"otlp,omitempty"`
	LogTransport  string `json:"log_transport,omitempty"`
}

type BootstrapRepo struct {
	RepoID     string `json:"repo_id"`
	BaseSHA    string `json:"base_sha"`
	FetchDepth int    `json:"fetch_depth"`
	// Local additions so agentd can tell repo-mirror which upstream repository the id names.
	Provider string `json:"provider,omitempty"`
	Host     string `json:"host,omitempty"`
	Owner    string `json:"owner,omitempty"`
	Name     string `json:"name,omitempty"`
}

type WS02 struct {
	ArtifactPrefixes struct {
		Artifacts   string `json:"artifacts"`
		Transcripts string `json:"transcripts"`
		Session     string `json:"session"`
	} `json:"artifact_prefixes"`
	MaxOutputBytes int64 `json:"max_output_bytes"`
	Workspace      struct {
		LimitBytes  int64  `json:"limit_bytes"`
		Enforcement string `json:"enforcement"`
	} `json:"workspace"`
	Timeouts struct {
		StartupSeconds        int `json:"startup_seconds"`
		ActiveDeadlineSeconds int `json:"active_deadline_seconds"`
		FinalizeSeconds       int `json:"finalize_seconds"`
	} `json:"timeouts"`
	Role            string   `json:"role"`
	RequiredOutputs []string `json:"required_outputs"`
	Redaction       struct {
		RulesetVersion string `json:"ruleset_version"`
	} `json:"redaction"`
	// Local additions used by the uploader.
	TenantID        string `json:"tenant_id,omitempty"`
	JobID           string `json:"job_id,omitempty"`
	ReleaseBundleID string `json:"release_bundle_id,omitempty"`
	AgentRun        string `json:"agentrun,omitempty"`
}

// LocalBootstrap carries runner-local-only settings (S3 endpoint for sandboxes).
type LocalBootstrap struct {
	S3Endpoint     string `json:"s3_endpoint"`
	S3Region       string `json:"s3_region,omitempty"`
	VerifyReadback bool   `json:"verify_readback,omitempty"`
	SendChecksum   bool   `json:"send_checksum,omitempty"`
}

// ReadBootstrap loads bootstrap.json from path (default BootstrapPath).
func ReadBootstrap(path string) (*Bootstrap, error) {
	if path == "" {
		path = BootstrapPath
		if p := os.Getenv("REPLICANT_BOOTSTRAP_FILE"); p != "" {
			path = p
		}
	}
	b, err := os.ReadFile(path)
	if err != nil {
		return nil, err
	}
	var bs Bootstrap
	return &bs, json.Unmarshal(b, &bs)
}

// InputsManifest is replicant.run-inputs/v1.
type InputsManifest struct {
	Schema                string        `json:"schema"`
	TenantID              string        `json:"tenant_id"`
	JobID                 string        `json:"job_id"`
	RunID                 string        `json:"run_id"`
	Role                  string        `json:"role"`
	ReleaseBundleID       string        `json:"release_bundle_id"`
	CreatedAt             string        `json:"created_at"`
	AgentDefinition       PinnedRef     `json:"agent_definition"`
	EnvironmentDefinition PinnedRef     `json:"environment_definition"`
	MemorySnapshotID      string        `json:"memory_snapshot_id,omitempty"`
	MemoryEntries         []string      `json:"memory_entries,omitempty"`
	Objects               []InputObject `json:"objects"`
}

type PinnedRef struct {
	Name    string `json:"name"`
	Version int64  `json:"version"`
	Digest  string `json:"digest"`
}

type InputObject struct {
	Name            string          `json:"name"`
	Type            string          `json:"type"`
	BucketClass     string          `json:"bucket_class"`
	Key             string          `json:"key"`
	SHA256          string          `json:"sha256"`
	SizeBytes       int64           `json:"size_bytes"`
	ContentType     string          `json:"content_type"`
	ContentEncoding string          `json:"content_encoding,omitempty"`
	Place           string          `json:"place"`
	Path            string          `json:"path,omitempty"`
	Session         json.RawMessage `json:"session,omitempty"`
}

// OutputsIndex is replicant.run-outputs/v1.
type OutputsIndex struct {
	Schema   string        `json:"schema"`
	RunID    string        `json:"run_id"`
	ExitCode int           `json:"exit_code"`
	Partial  bool          `json:"partial"`
	Result   string        `json:"result,omitempty"`
	Entries  []OutputEntry `json:"entries"`
}

type OutputEntry struct {
	Type            string         `json:"type"`
	BucketClass     string         `json:"bucket_class"`
	Path            string         `json:"path"`
	Name            string         `json:"name"`
	ContentType     string         `json:"content_type"`
	ContentEncoding string         `json:"content_encoding,omitempty"`
	SHA256          string         `json:"sha256,omitempty"`
	Required        bool           `json:"required"`
	Redaction       string         `json:"redaction,omitempty"`
	Attributes      map[string]any `json:"attributes,omitempty"`
	Producer        string         `json:"producer,omitempty"`
}

// ArtifactType is a row of the C-ART §4 registry needed by run producers.
type ArtifactType struct {
	BucketClass string
	ContentType string
	MaxBytes    int64
	Retention   string
	Redaction   string
	Text        bool
}

// Types a run may produce (run-outputs.schema.json enum) plus the inputs/control types.
var Types = map[string]ArtifactType{
	"run_result":         {"artifacts", "application/json", 1 << 20, "r2y", "required", true},
	"diff_bundle":        {"artifacts", "application/vnd.replicant.diff-bundle.v1+tar+zstd", 256 << 20, "r2y", "scan-block", false},
	"handoff":            {"artifacts", "application/json", 2 << 20, "r2y", "required", true},
	"plan":               {"artifacts", "application/json", 2 << 20, "r2y", "required", true},
	"review_report":      {"artifacts", "application/json", 4 << 20, "r2y", "required", true},
	"cli_session_state":  {"session", "application/vnd.replicant.session.v1+tar+zstd", 2 << 30, "job37d", "raw-restricted", false},
	"workspace_archive":  {"session", "application/x-tar+zstd", 10 << 30, "r30d", "raw-restricted", false},
	"memory_candidates":  {"artifacts", "application/json", 1 << 20, "r1y", "required", true},
	"transcript":         {"transcripts", "application/x-ndjson", 512 << 20, "r90d", "required", false},
	"cli_events":         {"transcripts", "application/x-ndjson", 512 << 20, "r90d", "required", false},
	"sandbox_log":        {"transcripts", "text/plain", 256 << 20, "r90d", "required", false},
	"qa_report":          {"artifacts", "application/json", 16 << 20, "r2y", "required", true},
	"junit_xml":          {"artifacts", "application/xml", 64 << 20, "r1y", "required", true},
	"build_log":          {"artifacts", "text/plain", 256 << 20, "r90d", "required", true},
	"health_log":         {"artifacts", "text/plain", 64 << 20, "r90d", "required", true},
	"screenshot":         {"artifacts", "image/png", 16 << 20, "r1y", "required", false},
	"playwright_trace":   {"artifacts", "application/zip", 256 << 20, "r90d", "required", false},
	"video":              {"artifacts", "video/webm", 256 << 20, "r30d", "required", false},
	"a11y_report":        {"artifacts", "application/json", 8 << 20, "r1y", "n/a", true},
	"sbom":               {"artifacts", "application/spdx+json", 32 << 20, "r2y", "n/a", true},
	"image_metadata":     {"artifacts", "application/json", 1 << 20, "r2y", "n/a", true},
	"secret_scan_report": {"artifacts", "application/json", 1 << 20, "r2y", "n/a", true},
	"qa_generated_flow":  {"artifacts", "application/x-tar+zstd", 4 << 20, "r1y", "scan-block", false},
	"run_manifest":       {"artifacts", "application/json", 4 << 20, "r2y", "n/a", true},
	"steering_message":   {"artifacts", "application/json", 64 << 10, "r90d", "required", true},
	"steering_control":   {"artifacts", "application/json", 4 << 10, "r90d", "n/a", true},
	"agentrun_record":    {"artifacts", "application/json", 64 << 10, "r2y", "n/a", true},
}

// WriteJSONAtomic writes v to path via temp + rename.
func WriteJSONAtomic(path string, v any, mode os.FileMode) error {
	b, err := json.MarshalIndent(v, "", "  ")
	if err != nil {
		return err
	}
	tmp := path + ".tmp"
	if err := os.WriteFile(tmp, b, mode); err != nil {
		return err
	}
	return os.Rename(tmp, path)
}
