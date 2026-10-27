package sandbox

import "encoding/json"

// RunPlan is the subset of replicant.run-plan/v1 that the entrypoint, agentd and replicant-qa read.
type RunPlan struct {
	Schema           string                `json:"schema"`
	RunID            string                `json:"run_id"`
	Role             string                `json:"role"`
	Agent            AgentSpec             `json:"agent"`
	Effective        Effective             `json:"effective"`
	Prompts          map[string]PromptText `json:"prompts"`
	Workspace        PlanWorkspace         `json:"workspace"`
	Environment      PlanEnvironment       `json:"environment"`
	RepoInstructions string                `json:"repo_instructions,omitempty"`
}

type PromptText struct {
	Text   string `json:"text"`
	SHA256 string `json:"sha256"`
}

type AgentSpec struct {
	Roles         []string          `json:"roles"`
	WorkloadClass string            `json:"workloadClass"`
	CLI           string            `json:"cli"`
	CLIImage      CLIImage          `json:"cliImage"`
	Model         Model             `json:"model"`
	Prompts       AgentPrompts      `json:"prompts"`
	Tools         Tools             `json:"tools"`
	MCPServers    []MCPServer       `json:"mcpServers"`
	Limits        AgentLimits       `json:"limits"`
	Gateway       GatewayCfg        `json:"gateway"`
	Env           map[string]string `json:"env,omitempty"`
	Steering      struct {
		Live bool `json:"live"`
	} `json:"steering"`
	Session struct {
		Capture   string `json:"capture"`
		Resumable bool   `json:"resumable"`
	} `json:"session"`
	OutputContract OutputContract  `json:"outputContract"`
	ClaudeCode     json.RawMessage `json:"claudeCode,omitempty"`
	QAHarness      *QAHarness      `json:"qaHarness,omitempty"`
}

type AgentPrompts struct {
	Role struct {
		Ref  string `json:"ref"`
		Mode string `json:"mode"`
	} `json:"role"`
}

type CLIImage struct {
	Image             string `json:"image"`
	Version           string `json:"version"`
	EntrypointVersion string `json:"entrypointVersion"`
}

type Model struct {
	Primary    string   `json:"primary"`
	Fallbacks  []string `json:"fallbacks,omitempty"`
	Small      string   `json:"small,omitempty"`
	Subagent   string   `json:"subagent,omitempty"`
	Parameters struct {
		MaxOutputTokens int `json:"maxOutputTokens,omitempty"`
		ThinkingTokens  int `json:"thinkingTokens,omitempty"`
	} `json:"parameters"`
}

type Tools struct {
	Read      bool     `json:"read"`
	Edit      bool     `json:"edit"`
	EditPaths []string `json:"editPaths,omitempty"`
	Shell     struct {
		Mode                  string   `json:"mode"`
		Allow                 []string `json:"allow,omitempty"`
		Deny                  []string `json:"deny,omitempty"`
		DefaultTimeoutSeconds int      `json:"defaultTimeoutSeconds,omitempty"`
		MaxTimeoutSeconds     int      `json:"maxTimeoutSeconds,omitempty"`
		MaxOutputChars        int      `json:"maxOutputChars,omitempty"`
	} `json:"shell"`
	Web struct {
		Fetch  bool `json:"fetch"`
		Search bool `json:"search"`
	} `json:"web"`
	Subagents struct {
		Enabled bool `json:"enabled"`
	} `json:"subagents"`
	DenyTools []string `json:"denyTools,omitempty"`
}

type MCPServer struct {
	Name    string `json:"name"`
	Service string `json:"service"`
	Tools   struct {
		Allow []string `json:"allow,omitempty"`
		Deny  []string `json:"deny,omitempty"`
	} `json:"tools"`
	Required           bool `json:"required"`
	ToolTimeoutSeconds int  `json:"toolTimeoutSeconds,omitempty"`
}

type AgentLimits struct {
	MaxTurns        int     `json:"maxTurns"`
	MaxWallSeconds  int     `json:"maxWallSeconds"`
	MaxCostUSD      float64 `json:"maxCostUsd"`
	MaxModelCalls   int     `json:"maxModelCalls"`
	StartupSeconds  int     `json:"startupSeconds"`
	FinalizeSeconds int     `json:"finalizeSeconds"`
	CLIGraceSeconds int     `json:"cliGraceSeconds"`
	MaxInputTokens  int64   `json:"maxInputTokens,omitempty"`
	MaxOutputTokens int64   `json:"maxOutputTokens,omitempty"`
}

type GatewayCfg struct {
	Protocol string `json:"protocol"`
	Retry    struct {
		MaxAttempts              int `json:"maxAttempts"`
		MaxCumulativeWaitSeconds int `json:"maxCumulativeWaitSeconds"`
		BaseDelayMs              int `json:"baseDelayMs"`
		MaxDelayMs               int `json:"maxDelayMs"`
	} `json:"retry"`
	RequestTimeoutSeconds   int     `json:"requestTimeoutSeconds"`
	SoftBudgetFraction      float64 `json:"softBudgetFraction"`
	UnavailableAfterSeconds int     `json:"unavailableAfterSeconds"`
}

type OutputContract struct {
	RoleResultSchema   string            `json:"roleResultSchema"`
	StructuredResult   string            `json:"structuredResult"`
	Outputs            map[string]string `json:"outputs"`
	DiffPolicy         DiffPolicy        `json:"diffPolicy"`
	WorkspaceIntegrity string            `json:"workspaceIntegrity"`
}

type DiffPolicy struct {
	ForbiddenPaths        []string `json:"forbiddenPaths"`
	OnForbiddenPath       string   `json:"onForbiddenPath"`
	MaxFilesChanged       int      `json:"maxFilesChanged"`
	MaxPatchBytes         int64    `json:"maxPatchBytes"`
	AllowBinary           bool     `json:"allowBinary"`
	AllowSubmoduleChanges bool     `json:"allowSubmoduleChanges"`
	AllowModeChanges      bool     `json:"allowModeChanges"`
}

type QAHarness struct {
	Version    string `json:"version,omitempty"`
	AgentTasks bool   `json:"agentTasks,omitempty"`
}

type Effective struct {
	BudgetUSD             float64  `json:"budget_usd"`
	JobBudgetRemainingUSD float64  `json:"job_budget_remaining_usd,omitempty"`
	MaxTurns              int      `json:"max_turns"`
	ActiveDeadlineSeconds int      `json:"active_deadline_seconds"`
	FinalizeSeconds       int      `json:"finalize_seconds,omitempty"`
	SteeringLive          bool     `json:"steering_live"`
	Models                []string `json:"models"`
	ResumeSession         string   `json:"resume_session,omitempty"`
	Audiences             []string `json:"audiences"`
	Deterministic         bool     `json:"deterministic,omitempty"`
	GitIdentityDate       string   `json:"git_identity_date,omitempty"`
	Scopes                []string `json:"scopes"`
	NetworkProfile        string   `json:"network_profile,omitempty"`
}

type PlanWorkspace struct {
	RepoPath   string `json:"repo_path"`
	BaseSHA    string `json:"base_sha"`
	BranchName string `json:"branch_name,omitempty"`
}

type PlanEnvironment struct {
	CodebaseType   string            `json:"codebase_type"`
	BootstrapSteps []Step            `json:"bootstrap_steps"`
	Env            map[string]string `json:"env"`
	PackageMirrors []string          `json:"package_mirrors"`
	Caches         []Cache           `json:"caches"`
	Services       []json.RawMessage `json:"services"`
}

type Step struct {
	Name           string `json:"name"`
	Run            string `json:"run"`
	TimeoutSeconds int    `json:"timeoutSeconds,omitempty"`
	OnFailure      string `json:"onFailure,omitempty"`
	When           *struct {
		PathsExist []string `json:"pathsExist,omitempty"`
	} `json:"when,omitempty"`
}

type Cache struct {
	Name string `json:"name"`
	Path string `json:"path"`
	Kind string `json:"kind"`
	Env  string `json:"env,omitempty"`
}
