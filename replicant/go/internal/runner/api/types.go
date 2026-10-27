// Package api holds the C-RUNNER 3.0.0 wire types (runner-api.openapi.yaml) shared by runner-local,
// the sandbox binaries and repo-mirror.
package api

import (
	"encoding/json"
	"net/http"
	"time"
)

type DefinitionRef struct {
	Kind    string `json:"kind"`
	Name    string `json:"name"`
	Version int64  `json:"version"`
	Digest  string `json:"digest"`
}

type Repo struct {
	Provider   string `json:"provider"`
	Host       string `json:"host"`
	Owner      string `json:"owner"`
	Name       string `json:"name"`
	RepoID     string `json:"repo_id"`
	BaseRef    string `json:"base_ref,omitempty"`
	BaseSHA    string `json:"base_sha"`
	FetchDepth *int32 `json:"fetch_depth,omitempty"`
}

type Inputs struct {
	ManifestURI    string `json:"manifest_uri"`
	ManifestSHA256 string `json:"manifest_sha256"`
}

type ArtifactPrefixes struct {
	Artifacts   string `json:"artifacts"`
	Transcripts string `json:"transcripts"`
	Session     string `json:"session"`
}

type Timeouts struct {
	StartupSeconds        int32 `json:"startup_seconds"`
	ActiveDeadlineSeconds int32 `json:"active_deadline_seconds"`
	FinalizeSeconds       int32 `json:"finalize_seconds"`
}

type Limits struct {
	MaxCostUSD     *float64 `json:"max_cost_usd,omitempty"`
	MaxOutputBytes *int64   `json:"max_output_bytes,omitempty"`
}

type Steering struct {
	Enabled bool `json:"enabled,omitempty"`
}

type TraceContext struct {
	Traceparent string `json:"traceparent"`
	Tracestate  string `json:"tracestate,omitempty"`
}

type Callback struct {
	Kind       string `json:"kind"`
	Namespace  string `json:"namespace"`
	WorkflowID string `json:"workflow_id"`
	SignalName string `json:"signal_name"`
}

// RunSpec is the createRun body.
type RunSpec struct {
	Schema                string           `json:"schema"`
	TenantID              string           `json:"tenant_id"`
	JobID                 string           `json:"job_id"`
	WorkflowID            string           `json:"workflow_id"`
	LogicalActivityID     string           `json:"logical_activity_id"`
	Role                  string           `json:"role"`
	WorkloadClass         string           `json:"workload_class"`
	WorkflowType          string           `json:"workflow_type,omitempty"`
	ReleaseBundleID       string           `json:"release_bundle_id"`
	AgentDefinition       DefinitionRef    `json:"agent_definition"`
	EnvironmentDefinition DefinitionRef    `json:"environment_definition"`
	ResourceProfile       string           `json:"resource_profile,omitempty"`
	Repo                  Repo             `json:"repo"`
	AdditionalRepos       []Repo           `json:"additional_repos,omitempty"`
	Inputs                Inputs           `json:"inputs"`
	ArtifactPrefixes      ArtifactPrefixes `json:"artifact_prefixes"`
	IsolationHint         string           `json:"isolation_hint,omitempty"`
	NetworkProfile        string           `json:"network_profile"`
	Timeouts              Timeouts         `json:"timeouts"`
	Limits                *Limits          `json:"limits,omitempty"`
	Steering              *Steering        `json:"steering,omitempty"`
	Priority              string           `json:"priority"`
	TraceContext          *TraceContext    `json:"trace_context,omitempty"`
	RunGrant              string           `json:"run_grant"`
	Callback              Callback         `json:"callback"`
	Extra                 map[string]any   `json:"-"`
}

func (r RunSpec) ResourceProfileOrDefault() string {
	if r.ResourceProfile == "" {
		return "default"
	}
	return r.ResourceProfile
}

type Condition struct {
	Type               string    `json:"type"`
	Status             string    `json:"status"`
	Reason             string    `json:"reason"`
	Message            string    `json:"message,omitempty"`
	LastTransitionTime time.Time `json:"last_transition_time"`
	ObservedGeneration int64     `json:"observed_generation,omitempty"`
}

type Failure struct {
	Reason    string `json:"reason"`
	Message   string `json:"message,omitempty"`
	Retryable bool   `json:"retryable"`
}

type AgentRunRef struct {
	Namespace string `json:"namespace"`
	Name      string `json:"name"`
}

type Timestamps struct {
	CreatedAt        time.Time  `json:"created_at"`
	ScheduledAt      *time.Time `json:"scheduled_at,omitempty"`
	WorkspaceReadyAt *time.Time `json:"workspace_ready_at,omitempty"`
	AgentStartedAt   *time.Time `json:"agent_started_at,omitempty"`
	AgentExitedAt    *time.Time `json:"agent_exited_at,omitempty"`
	FinishedAt       *time.Time `json:"finished_at,omitempty"`
}

type RunStatus struct {
	RunID               string      `json:"run_id"`
	TenantID            string      `json:"tenant_id"`
	JobID               string      `json:"job_id"`
	IdempotencyKey      string      `json:"idempotency_key"`
	SpecDigest          string      `json:"spec_digest"`
	AgentRun            AgentRunRef `json:"agentrun"`
	Isolation           string      `json:"isolation,omitempty"`
	Phase               string      `json:"phase"`
	Terminal            bool        `json:"terminal"`
	Failure             *Failure    `json:"failure,omitempty"`
	Conditions          []Condition `json:"conditions"`
	Timestamps          Timestamps  `json:"timestamps"`
	ArtifactManifestURI string      `json:"artifact_manifest_uri,omitempty"`
	ResultURI           string      `json:"result_uri,omitempty"`
	ObservedGeneration  int64       `json:"observed_generation"`
}

type RunList struct {
	Runs          []RunStatus `json:"runs"`
	NextPageToken string      `json:"next_page_token,omitempty"`
}

type CancelRequest struct {
	Reason      string `json:"reason"`
	Detail      string `json:"detail,omitempty"`
	RequestedBy string `json:"requested_by,omitempty"`
}

type RunMessage struct {
	MessageID string    `json:"message_id"`
	Text      string    `json:"text"`
	Author    string    `json:"author"`
	SentAt    time.Time `json:"sent_at"`
}

type RunMessageStatus struct {
	MessageID string     `json:"message_id"`
	RunID     string     `json:"run_id"`
	Seq       int        `json:"seq,omitempty"`
	State     string     `json:"state"`
	UpdatedAt *time.Time `json:"updated_at,omitempty"`
}

type InboxMessage struct {
	Seq       int       `json:"seq"`
	MessageID string    `json:"message_id"`
	Text      string    `json:"text"`
	Author    string    `json:"author"`
	SentAt    time.Time `json:"sent_at"`
}

type InboxPoll struct {
	Messages []InboxMessage `json:"messages"`
}

type AckRequest struct {
	State  string `json:"state"`
	Detail string `json:"detail,omitempty"`
}

type DiskReport struct {
	State      string `json:"state"`
	UsedBytes  int64  `json:"used_bytes"`
	LimitBytes int64  `json:"limit_bytes"`
	PeakBytes  int64  `json:"peak_bytes"`
}

type HeartbeatRequest struct {
	Disk *DiskReport `json:"disk,omitempty"`
}

type HeartbeatResponse struct {
	Continue     bool    `json:"continue"`
	CancelReason *string `json:"cancel_reason"`
}

type Revocation struct {
	RunID  string    `json:"run_id"`
	JobID  string    `json:"job_id"`
	At     time.Time `json:"at"`
	Reason string    `json:"reason"`
}

type RevocationList struct {
	Revoked    []Revocation `json:"revoked"`
	NextCursor string       `json:"next_cursor"`
}

type TokenEntry struct {
	Token     string    `json:"token"`
	ExpiresAt time.Time `json:"expires_at"`
}

type RunTokenResponse struct {
	Tokens map[string]TokenEntry `json:"tokens"`
}

type AwsContainerCredentials struct {
	AccessKeyId     string    `json:"AccessKeyId"`
	SecretAccessKey string    `json:"SecretAccessKey"`
	Token           string    `json:"Token"`
	Expiration      time.Time `json:"Expiration"`
}

type RunStatusNotification struct {
	Schema              string    `json:"schema"`
	NotificationID      string    `json:"notification_id"`
	RunID               string    `json:"run_id"`
	JobID               string    `json:"job_id"`
	TenantID            string    `json:"tenant_id"`
	IdempotencyKey      string    `json:"idempotency_key"`
	Phase               string    `json:"phase"`
	Terminal            bool      `json:"terminal"`
	Reason              string    `json:"reason,omitempty"`
	ObservedGeneration  int64     `json:"observed_generation"`
	ObservedAt          time.Time `json:"observed_at"`
	ArtifactManifestURI string    `json:"artifact_manifest_uri,omitempty"`
	Traceparent         string    `json:"traceparent,omitempty"`
}

// RunTokenClaims (C-RUNNER RunTokenClaims, one audience per token).
type RunTokenClaims struct {
	Iss             string   `json:"iss"`
	Sub             string   `json:"sub"`
	Aud             string   `json:"aud"`
	Exp             int64    `json:"exp"`
	Iat             int64    `json:"iat"`
	Jti             string   `json:"jti"`
	TenantID        string   `json:"tenant_id"`
	JobID           string   `json:"job_id"`
	RunID           string   `json:"run_id"`
	Role            string   `json:"role"`
	ReleaseBundleID string   `json:"release_bundle_id,omitempty"`
	RepoIDs         []string `json:"repo_ids,omitempty"`
	Scopes          []string `json:"scopes"`
	BudgetUSD       *float64 `json:"budget_usd,omitempty"`
	ActingFor       string   `json:"acting_for"`
	Grant           string   `json:"grant"`
	GrantJti        string   `json:"grant_jti"`
}

// RunGrantClaims (C-RUNNER RunGrantClaims).
type RunGrantClaims struct {
	Iss        string   `json:"iss"`
	Typ        string   `json:"typ"`
	Jti        string   `json:"jti"`
	Iat        int64    `json:"iat"`
	Exp        int64    `json:"exp"`
	TenantID   string   `json:"tenant_id"`
	JobID      string   `json:"job_id"`
	RunID      string   `json:"run_id"`
	Role       string   `json:"role"`
	RepoIDs    []string `json:"repo_ids"`
	Audiences  []string `json:"audiences"`
	Scopes     []string `json:"scopes"`
	BudgetUSD  float64  `json:"budget_usd"`
	ActingFor  string   `json:"acting_for"`
	SpecDigest string   `json:"spec_digest"`
}

// Audiences (closed enum).
const (
	AudGateway  = "ai-gateway"
	AudMemory   = "memory-mcp"
	AudContext  = "context-mcp"
	AudMirror   = "repo-mirror"
	AudSandbox  = "runner-sandbox"
	GrantTyp    = "replicant-run-grant+jwt"
	TokenTyp    = "JWT"
	NotifSchema = "replicant.msg.run-status-changed/v1"
)

// AllAudiences in canonical order.
var AllAudiences = []string{AudGateway, AudMemory, AudContext, AudMirror, AudSandbox}

// Problem is an RFC 9457 problem with the C-RUNNER extensions.
type Problem struct {
	Type              string      `json:"type"`
	Title             string      `json:"title"`
	Status            int         `json:"status"`
	Detail            string      `json:"detail,omitempty"`
	Instance          string      `json:"instance,omitempty"`
	Code              string      `json:"code"`
	Retryable         bool        `json:"retryable"`
	RetryAfterSeconds int         `json:"retry_after_seconds,omitempty"`
	QuotaUsed         *int        `json:"quota_used,omitempty"`
	QuotaLimit        *int        `json:"quota_limit,omitempty"`
	RunID             string      `json:"run_id,omitempty"`
	Violations        []Violation `json:"violations,omitempty"`
}

type Violation struct {
	Field   string `json:"field"`
	Rule    string `json:"rule"`
	Message string `json:"message,omitempty"`
}

func (p *Problem) Error() string { return p.Code + ": " + p.Detail }

var problemStatus = map[string]int{
	"invalid_request": 400, "missing_idempotency_key": 400, "unauthenticated": 401, "forbidden": 403,
	"run_not_found": 404, "steering_not_supported": 409, "run_not_active": 409, "run_expired": 410,
	"idempotency_key_reuse": 422, "admission_denied": 422, "run_grant_invalid": 422,
	"definition_not_resolvable": 422, "tenant_quota_exceeded": 429, "rate_limited": 429, "internal": 500,
	"kubernetes_unavailable": 503, "runner_unavailable": 503,
}

var problemRetryable = map[string]bool{
	"unauthenticated": true, "tenant_quota_exceeded": true, "rate_limited": true, "internal": true,
	"kubernetes_unavailable": true, "runner_unavailable": true,
}

// NewProblem builds a Problem for a C-RUNNER error code.
func NewProblem(code, detail string) *Problem {
	st := problemStatus[code]
	if st == 0 {
		st = 500
	}
	return &Problem{
		Type:      "https://errors.replicant.example.com/runner/" + code,
		Title:     http.StatusText(st),
		Status:    st,
		Detail:    detail,
		Code:      code,
		Retryable: problemRetryable[code],
	}
}

// WriteProblem writes the problem as application/problem+json.
func WriteProblem(w http.ResponseWriter, p *Problem) {
	w.Header().Set("Content-Type", "application/problem+json")
	if p.RetryAfterSeconds > 0 {
		w.Header().Set("Retry-After", itoa(p.RetryAfterSeconds))
	}
	w.WriteHeader(p.Status)
	_ = json.NewEncoder(w).Encode(p)
}

// WriteJSON writes v with status.
func WriteJSON(w http.ResponseWriter, status int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(v)
}

func itoa(i int) string {
	b, _ := json.Marshal(i)
	return string(b)
}
