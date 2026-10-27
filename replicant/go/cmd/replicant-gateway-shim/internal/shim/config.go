package shim

import (
	"fmt"
	"math"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"time"

	"gopkg.in/yaml.v3"
)

// Route is one `routes.<name>` entry of gateway-shim.yaml.
type Route struct {
	Prefix          string            `yaml:"prefix"`
	Upstream        string            `yaml:"upstream"`
	UpstreamByModel map[string]string `yaml:"upstream_by_model"`
	Protocol        string            `yaml:"protocol"`
}

// FileConfig mirrors the keys of spec/11-local-dev/compose/config/gateway-shim/gateway-shim.yaml (gateway.md §4.1).
type FileConfig struct {
	Listen    string                  `yaml:"listen"`
	Routes    map[string]Route        `yaml:"routes"`
	ModelMap  map[string]string       `yaml:"model_map"`
	Catalog   map[string]CatalogEntry `yaml:"catalog"`
	PriceBook struct {
		Source string `yaml:"source"`
		Path   string `yaml:"path"`
	} `yaml:"price_book"`
	Quotas struct {
		RefreshInterval string `yaml:"refresh_interval"`
	} `yaml:"quotas"`
	Auth struct {
		RunToken struct {
			Audience    string `yaml:"audience"`
			IssuersEnv  string `yaml:"issuers_env"`
			JWKSURLsEnv string `yaml:"jwks_urls_env"`
		} `yaml:"run_token"`
		RunGrant struct {
			IssuerEnv        string `yaml:"issuer_env"`
			JWKSURLEnv       string `yaml:"jwks_url_env"`
			RevocationURLEnv string `yaml:"revocation_url_env"`
		} `yaml:"run_grant"`
		ServiceTokensFileEnv string `yaml:"service_tokens_file_env"`
	} `yaml:"auth"`
	Budgets struct {
		DevDefaults struct {
			RunUSD float64 `yaml:"run_usd"`
			JobUSD float64 `yaml:"job_usd"`
		} `yaml:"dev_defaults"`
		Embeddings struct {
			PerTenantDayUSD       float64 `yaml:"per_tenant_day_usd"`
			ReembedBackfillUSDMax float64 `yaml:"reembed_backfill_usd_max"`
		} `yaml:"embeddings"`
	} `yaml:"budgets"`
	Revocations struct {
		ExecFeedURLsEnv        string `yaml:"exec_feed_urls_env"`
		ExecFeedTokenFileEnv   string `yaml:"exec_feed_token_file_env"`
		ExecFeedPollSecondsEnv string `yaml:"exec_feed_poll_seconds_env"`
	} `yaml:"revocations"`
	UsageCopy struct {
		S3URIEnv string `yaml:"s3_uri_env"`
	} `yaml:"usage_copy"`
	Local *struct {
		DevKeysEnv       []string `yaml:"dev_keys_env"`
		CassetteRootsEnv string   `yaml:"cassette_roots_env"`
		CassetteLayout   string   `yaml:"cassette_layout"`
		Miss             struct {
			Status int    `yaml:"status"`
			Code   string `yaml:"code"`
		} `yaml:"miss"`
		RecordRoot string `yaml:"record_root"`
	} `yaml:"local"`
}

// CatalogEntry is an optional `catalog.<model>` entry (GW-16).
type CatalogEntry struct {
	RetiresAt       string `yaml:"retires_at"`
	RetiresAtSource string `yaml:"retires_at_source"`
	ZDR             bool   `yaml:"zdr"`
}

// Price is USD per million tokens.
type Price struct {
	Input      float64 `yaml:"input"`
	Output     float64 `yaml:"output"`
	CacheRead  float64 `yaml:"cache_read"`
	CacheWrite float64 `yaml:"cache_write"`
}

// Config is the resolved runtime configuration.
type Config struct {
	File        FileConfig
	ConfigDir   string
	Backend     string // enterprise | passthrough | record | replay | script
	ReplayMatch string
	StateMode   string // local (v0) | valkey
	Env         string // REPLICANT_ENV

	Prices map[string]Price

	RunTokenAudience        string
	RunTokenIssuers         []string
	RunTokenJWKSURLs        []string
	GrantIssuer             string
	GrantJWKSURL            string
	GrantRevocationURL      string
	GrantRevocationPoll     time.Duration
	ExecRevocationURLs      []string
	ExecRevocationTokenFile string
	ExecRevocationPoll      time.Duration

	ServiceTokensFile string
	CassetteRoots     map[string]string

	ControlPlaneURL       string
	ControlPlaneTokenFile string
	QuotaRefresh          time.Duration
	LocalQuota            *TenantQuota // REPLICANT_GATEWAY_LOCAL_QUOTA fallback (local only)

	UsageURL       string
	UsageTokenFile string
	UsageS3URI     string
	UsageLogDir    string
	S3Endpoint     string
	S3PathStyle    bool
	PodName        string

	ScriptDefaultSet string // REPLICANT_GATEWAY_SCRIPT_DEFAULT_SET

	DevRunMicros int64
	DevJobMicros int64
}

func envList(name string) []string {
	v := strings.TrimSpace(os.Getenv(name))
	if v == "" {
		return nil
	}
	var out []string
	for _, p := range strings.Split(v, ",") {
		if p = strings.TrimSpace(p); p != "" {
			out = append(out, p)
		}
	}
	return out
}

func envSeconds(name string, def int) time.Duration {
	if v := os.Getenv(name); v != "" {
		if n, err := strconv.Atoi(v); err == nil && n > 0 {
			return time.Duration(n) * time.Second
		}
	}
	return time.Duration(def) * time.Second
}

// USDToMicros converts dollars to integer micro-USD.
func USDToMicros(usd float64) int64 { return int64(math.Round(usd * 1e6)) }

// LoadConfig reads the YAML file and the environment named by it.
func LoadConfig(path, backend, replayMatch, stateMode string) (*Config, error) {
	b, err := os.ReadFile(path)
	if err != nil {
		return nil, fmt.Errorf("config: %w", err)
	}
	var fc FileConfig
	if err := yaml.Unmarshal(b, &fc); err != nil {
		return nil, fmt.Errorf("config: %w", err)
	}
	c := &Config{File: fc, ConfigDir: filepath.Dir(path), Backend: backend, ReplayMatch: replayMatch, StateMode: stateMode}
	switch backend {
	case "enterprise", "passthrough", "script":
	case "record", "replay":
		return nil, fmt.Errorf("--upstream-backend=%s is not implemented in v0 (script and passthrough only)", backend)
	default:
		return nil, fmt.Errorf("unknown --upstream-backend %q", backend)
	}
	if backend == "enterprise" && fc.Local != nil {
		return nil, fmt.Errorf("config: the `local` block is rejected with the enterprise backend")
	}
	if c.StateMode == "" {
		c.StateMode = "local"
	}
	if c.StateMode != "local" {
		return nil, fmt.Errorf("--state=%s is not implemented in v0 (use --state=local, see DEVIATIONS)", c.StateMode)
	}
	if c.File.Listen == "" {
		c.File.Listen = "0.0.0.0:8420"
	}
	c.Env = os.Getenv("REPLICANT_ENV")

	// price book
	pp := fc.PriceBook.Path
	if pp == "" {
		pp = filepath.Join(c.ConfigDir, "prices.yaml")
	}
	if _, err := os.Stat(pp); err != nil {
		alt := filepath.Join(c.ConfigDir, filepath.Base(pp))
		if _, err2 := os.Stat(alt); err2 == nil {
			pp = alt
		}
	}
	pb, err := os.ReadFile(pp)
	if err != nil {
		return nil, fmt.Errorf("price book: %w", err)
	}
	if err := yaml.Unmarshal(pb, &c.Prices); err != nil {
		return nil, fmt.Errorf("price book: %w", err)
	}

	c.RunTokenAudience = fc.Auth.RunToken.Audience
	if c.RunTokenAudience == "" {
		c.RunTokenAudience = "ai-gateway"
	}
	envOr := func(name, def string) string {
		if name == "" {
			return def
		}
		return name
	}
	c.RunTokenIssuers = envList(envOr(fc.Auth.RunToken.IssuersEnv, "REPLICANT_RUN_TOKEN_ISSUERS"))
	c.RunTokenJWKSURLs = envList(envOr(fc.Auth.RunToken.JWKSURLsEnv, "REPLICANT_RUN_TOKEN_JWKS_URLS"))
	if len(c.RunTokenIssuers) != len(c.RunTokenJWKSURLs) {
		return nil, fmt.Errorf("REPLICANT_RUN_TOKEN_ISSUERS and REPLICANT_RUN_TOKEN_JWKS_URLS must pair by position (%d vs %d)", len(c.RunTokenIssuers), len(c.RunTokenJWKSURLs))
	}
	c.GrantIssuer = os.Getenv(envOr(fc.Auth.RunGrant.IssuerEnv, "REPLICANT_RUN_GRANT_ISSUER"))
	c.GrantJWKSURL = os.Getenv(envOr(fc.Auth.RunGrant.JWKSURLEnv, "REPLICANT_RUN_GRANT_JWKS_URL"))
	c.GrantRevocationURL = os.Getenv(envOr(fc.Auth.RunGrant.RevocationURLEnv, "REPLICANT_RUN_GRANT_REVOCATION_URL"))
	c.GrantRevocationPoll = envSeconds("REPLICANT_RUN_GRANT_REVOCATION_POLL_SECONDS", 60)
	c.ExecRevocationURLs = envList(envOr(fc.Revocations.ExecFeedURLsEnv, "REPLICANT_EXEC_REVOCATIONS_URLS"))
	c.ExecRevocationTokenFile = os.Getenv(envOr(fc.Revocations.ExecFeedTokenFileEnv, "REPLICANT_EXEC_REVOCATIONS_TOKEN_FILE"))
	c.ExecRevocationPoll = envSeconds(envOr(fc.Revocations.ExecFeedPollSecondsEnv, "REPLICANT_EXEC_REVOCATIONS_POLL_SECONDS"), 10)
	c.ServiceTokensFile = os.Getenv(envOr(fc.Auth.ServiceTokensFileEnv, "REPLICANT_GATEWAY_SERVICE_TOKENS_FILE"))

	c.CassetteRoots = map[string]string{}
	rootsEnv := "REPLICANT_GATEWAY_CASSETTE_ROOTS"
	if fc.Local != nil && fc.Local.CassetteRootsEnv != "" {
		rootsEnv = fc.Local.CassetteRootsEnv
	}
	for _, kv := range envList(rootsEnv) {
		if k, v, ok := strings.Cut(kv, "="); ok {
			c.CassetteRoots[strings.TrimSpace(k)] = strings.TrimSpace(v)
		}
	}

	c.ControlPlaneURL = strings.TrimRight(os.Getenv("REPLICANT_CONTROL_PLANE_URL"), "/")
	c.ControlPlaneTokenFile = os.Getenv("REPLICANT_CONTROL_PLANE_TOKEN_FILE")
	c.QuotaRefresh = 60 * time.Second
	if d, err := time.ParseDuration(fc.Quotas.RefreshInterval); err == nil && d > 0 {
		c.QuotaRefresh = d
	}
	if v := os.Getenv("REPLICANT_GATEWAY_LOCAL_QUOTA"); v != "" {
		if backend == "enterprise" {
			return nil, fmt.Errorf("REPLICANT_GATEWAY_LOCAL_QUOTA is local only")
		}
		q, err := parseLocalQuota(v)
		if err != nil {
			return nil, err
		}
		c.LocalQuota = q
	}

	c.UsageURL = os.Getenv("REPLICANT_GATEWAY_USAGE_URL")
	c.UsageTokenFile = os.Getenv("REPLICANT_GATEWAY_USAGE_TOKEN_FILE")
	s3env := "REPLICANT_GATEWAY_USAGE_S3_URI"
	if fc.UsageCopy.S3URIEnv != "" {
		s3env = fc.UsageCopy.S3URIEnv
	}
	c.UsageS3URI = os.Getenv(s3env)
	c.UsageLogDir = os.Getenv("REPLICANT_GATEWAY_USAGE_LOG_DIR")
	c.S3Endpoint = os.Getenv("REPLICANT_S3_ENDPOINT")
	c.S3PathStyle = os.Getenv("REPLICANT_S3_FORCE_PATH_STYLE") == "true"
	c.PodName = os.Getenv("POD_NAME")
	if c.PodName == "" {
		c.PodName, _ = os.Hostname()
	}
	if c.PodName == "" {
		c.PodName = "gateway-shim"
	}
	c.ScriptDefaultSet = os.Getenv("REPLICANT_GATEWAY_SCRIPT_DEFAULT_SET")
	c.DevRunMicros = USDToMicros(fc.Budgets.DevDefaults.RunUSD)
	c.DevJobMicros = USDToMicros(fc.Budgets.DevDefaults.JobUSD)
	return c, nil
}

// parseLocalQuota parses "monthly=100,daily=50,platform_daily=500,platform_monthly=2000".
func parseLocalQuota(v string) (*TenantQuota, error) {
	q := &TenantQuota{Version: 0}
	for _, kv := range strings.Split(v, ",") {
		k, val, ok := strings.Cut(strings.TrimSpace(kv), "=")
		if !ok {
			return nil, fmt.Errorf("REPLICANT_GATEWAY_LOCAL_QUOTA: bad item %q", kv)
		}
		f, err := strconv.ParseFloat(val, 64)
		if err != nil {
			return nil, fmt.Errorf("REPLICANT_GATEWAY_LOCAL_QUOTA: %w", err)
		}
		switch k {
		case "monthly":
			q.MonthlyBudgetUSD = f
		case "daily":
			q.DailyBudgetUSD = f
		case "platform_daily":
			q.PlatformDailyUSDMax = f
		case "platform_monthly":
			q.PlatformMonthlyUSDMax = f
		default:
			return nil, fmt.Errorf("REPLICANT_GATEWAY_LOCAL_QUOTA: unknown key %q", k)
		}
	}
	return q, nil
}

// UpstreamModel maps a gateway model id to the upstream name.
func (c *Config) UpstreamModel(gw string) string {
	if m, ok := c.File.ModelMap[gw]; ok {
		return m
	}
	if _, after, ok := strings.Cut(gw, "/"); ok {
		return after
	}
	return gw
}

// Cost prices a usage record in micro-USD (price is USD per 1M tokens, so tokens*price = micro-USD).
func (c *Config) Cost(model string, u Usage) (int64, bool) {
	p, ok := c.Prices[model]
	if !ok {
		return 0, false
	}
	cr, cw := p.CacheRead, p.CacheWrite
	if cr == 0 {
		cr = p.Input
	}
	if cw == 0 {
		cw = p.Input
	}
	v := float64(u.InputTokens)*p.Input + float64(u.OutputTokens)*p.Output +
		float64(u.CacheReadTokens)*cr + float64(u.CacheWriteTokens)*cw
	return int64(math.Ceil(v)), true
}

// Estimate is gateway.md §6.2: price_in × bytes/3 + price_out × min(max_tokens, 32000).
func (c *Config) Estimate(model string, bodyBytes int, maxTokens int) int64 {
	p := c.Prices[model]
	if maxTokens <= 0 || maxTokens > 32000 {
		maxTokens = 32000
	}
	return int64(math.Ceil(p.Input*float64(bodyBytes)/3 + p.Output*float64(maxTokens)))
}
