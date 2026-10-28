package shim

import (
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"regexp"
	"strings"
	"sync"
	"time"

	"example.com/replicant/internal/runner/jose"
	"gopkg.in/yaml.v3"
)

// RunClaims is C-RUNNER RunTokenClaims (2.x, CCR-09-01/02/03).
type RunClaims struct {
	Iss             string          `json:"iss"`
	Sub             string          `json:"sub"`
	Aud             json.RawMessage `json:"aud"`
	Exp             int64           `json:"exp"`
	Iat             int64           `json:"iat"`
	Jti             string          `json:"jti"`
	TenantID        string          `json:"tenant_id"`
	JobID           string          `json:"job_id"`
	RunID           string          `json:"run_id"`
	Role            string          `json:"role"`
	ReleaseBundleID string          `json:"release_bundle_id"`
	RepoIDs         []string        `json:"repo_ids"`
	Scopes          []string        `json:"scopes"`
	BudgetUSD       *float64        `json:"budget_usd"`
	ActingFor       string          `json:"acting_for"`
	Grant           string          `json:"grant"`
	GrantJti        string          `json:"grant_jti"`
}

// GrantClaims is C-RUNNER RunGrantClaims.
type GrantClaims struct {
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

// Caller is an authenticated principal.
type Caller struct {
	Kind    string // run | service
	Run     *RunClaims
	Service string   // svc_replicant-worker, ...
	Rights  []string // local service rights (see serviceRights)
}

// AuthError carries the replicant_code for a 401/403.
type AuthError struct {
	Status int
	Code   string
	Msg    string
}

func (e *AuthError) Error() string { return e.Code + ": " + e.Msg }

func unauth(format string, a ...any) *AuthError {
	return &AuthError{Status: 401, Code: "unauthenticated", Msg: fmt.Sprintf(format, a...)}
}

// Authenticator verifies run tokens (runner JWKS + core grant JWKS) and static local service tokens.
type Authenticator struct {
	audience    string
	issuers     map[string]*jose.KeySet
	grantIssuer string
	grantKeys   *jose.KeySet
	svcFile     string
	now         func() time.Time

	mu       sync.Mutex
	svc      map[string]string // token -> principal
	svcMtime time.Time
}

var (
	reRun    = regexp.MustCompile(`^run_[0-9a-hjkmnp-tv-z]{26}$`)
	reJob    = regexp.MustCompile(`^job_[0-9a-hjkmnp-tv-z]{26}$`)
	reTenant = regexp.MustCompile(`^[a-z][a-z0-9]*(-[a-z0-9]+)*$`)
	reRB     = regexp.MustCompile(`^rb_[0-9a-hjkmnp-tv-z]{26}$`)
	reAcct   = regexp.MustCompile(`^fgw:(eval|pipeline|proposal):[A-Za-z0-9_.:-]{1,120}$`)
)

// NewAuthenticator wires the key sets from config.
func NewAuthenticator(c *Config) *Authenticator {
	a := &Authenticator{audience: c.RunTokenAudience, issuers: map[string]*jose.KeySet{}, grantIssuer: c.GrantIssuer,
		svcFile: c.ServiceTokensFile, now: time.Now}
	for i, iss := range c.RunTokenIssuers {
		a.issuers[iss] = jose.NewKeySet(c.RunTokenJWKSURLs[i])
	}
	if c.GrantJWKSURL != "" {
		a.grantKeys = jose.NewKeySet(c.GrantJWKSURL)
	}
	return a
}

// KeyStatus reports JWKS cache state for /readyz.
func (a *Authenticator) KeyStatus() map[string]string {
	out := map[string]string{}
	for iss, ks := range a.issuers {
		ok, err := ks.Status()
		out["run_token:"+iss] = statusString(ok, err)
	}
	if a.grantKeys != nil {
		ok, err := a.grantKeys.Status()
		out["run_grant"] = statusString(ok, err)
	}
	return out
}

func statusString(ok bool, err error) string {
	switch {
	case ok:
		return "cached"
	case err != nil:
		return "error"
	default:
		return "not_fetched"
	}
}

func contains(xs []string, v string) bool {
	for _, x := range xs {
		if x == v {
			return true
		}
	}
	return false
}

func audMatches(raw json.RawMessage, want string) bool {
	var s string
	if json.Unmarshal(raw, &s) == nil {
		return s == want
	}
	var arr []string // CCR-09-01 N/N+1 window: array containing our audience
	if json.Unmarshal(raw, &arr) == nil {
		return contains(arr, want)
	}
	return false
}

// VerifyRunToken implements GW-2 (gateway.md §3) and C-RUNNER verifier obligations 1, 2 and 4.
func (a *Authenticator) VerifyRunToken(tok string) (*RunClaims, error) {
	p, err := jose.Parse(tok)
	if err != nil {
		return nil, unauth("malformed token")
	}
	payload := p.Payload
	var pre RunClaims
	if err := json.Unmarshal(payload, &pre); err != nil {
		return nil, unauth("malformed claims")
	}
	ks, ok := a.issuers[pre.Iss]
	if !ok {
		return nil, unauth("issuer not trusted")
	}
	if _, payload, err = ks.Verify(tok); err != nil {
		return nil, unauth("run token signature: %v", err)
	}
	var c RunClaims
	if err := json.Unmarshal(payload, &c); err != nil {
		return nil, unauth("malformed claims")
	}
	now := a.now().Unix()
	if !audMatches(c.Aud, a.audience) {
		return nil, unauth("audience is not %s", a.audience)
	}
	if c.Exp == 0 || now > c.Exp+30 {
		return nil, unauth("token expired")
	}
	if c.Iat > now+60 {
		return nil, unauth("token issued in the future")
	}
	if !reRun.MatchString(c.RunID) || !reJob.MatchString(c.JobID) || !reTenant.MatchString(c.TenantID) || c.Role == "" {
		return nil, unauth("token identity claims invalid")
	}
	if c.Sub != "" && c.Sub != "run:"+c.RunID {
		return nil, unauth("sub does not match run_id")
	}
	if !contains(c.Scopes, "gateway:invoke") {
		return nil, &AuthError{Status: 403, Code: "scope_missing", Msg: "token lacks gateway:invoke"}
	}
	if c.ActingFor == "" {
		return nil, unauth("acting_for missing")
	}
	if c.Grant == "" || c.GrantJti == "" {
		return nil, unauth("run grant missing")
	}
	if a.grantKeys == nil {
		return nil, unauth("run grant JWKS not configured")
	}
	gh, gp, err := a.grantKeys.Verify(c.Grant)
	if err != nil {
		return nil, unauth("run grant signature: %v", err)
	}
	var g GrantClaims
	if err := json.Unmarshal(gp, &g); err != nil {
		return nil, unauth("run grant claims malformed")
	}
	if gh.Typ != "" && gh.Typ != "replicant-run-grant+jwt" {
		return nil, unauth("run grant typ %q", gh.Typ)
	}
	if g.Typ != "replicant-run-grant+jwt" {
		return nil, unauth("run grant typ claim")
	}
	if a.grantIssuer != "" && g.Iss != a.grantIssuer {
		return nil, unauth("run grant issuer")
	}
	if g.Exp == 0 || now > g.Exp+30 {
		return nil, unauth("run grant expired")
	}
	if g.Jti != c.GrantJti {
		return nil, unauth("grant_jti mismatch")
	}
	if !contains(g.Audiences, a.audience) {
		return nil, unauth("grant does not cover %s", a.audience)
	}
	for _, s := range c.Scopes {
		if !contains(g.Scopes, s) {
			return nil, unauth("token scope %s outside grant", s)
		}
	}
	if !contains(g.Scopes, "gateway:invoke") {
		return nil, &AuthError{Status: 403, Code: "scope_missing", Msg: "grant lacks gateway:invoke"}
	}
	for _, r := range c.RepoIDs {
		if !contains(g.RepoIDs, r) {
			return nil, unauth("token repo outside grant")
		}
	}
	if c.BudgetUSD != nil && *c.BudgetUSD > g.BudgetUSD+1e-9 {
		return nil, unauth("token budget exceeds grant")
	}
	if c.Exp > g.Exp {
		return nil, unauth("token outlives grant")
	}
	if g.TenantID != c.TenantID || g.JobID != c.JobID || g.RunID != c.RunID || g.ActingFor != c.ActingFor || (g.Role != "" && g.Role != c.Role) {
		return nil, unauth("token identity differs from grant")
	}
	return &c, nil
}

// serviceRights are the local rights of each static service principal (prod: projected SA tokens, P-26).
var serviceRights = map[string][]string{
	"svc_replicant-worker":   {"policy", "job_budget", "worker_llm", "models"},
	"svc_control-plane-api":  {"revoke", "account", "models"},
	"svc_memory-service":     {"embedding"},
	"svc_eval-worker":        {"eval_judge"},
	"svc_release-controller": {"models"},
}

// localAllServices are local-only subjects holding every service right: PLATFORM generates one shared
// `gateway_service_token` (subject svc_replicant-services) used by replicant-worker, control-plane-api, memory-service
// and eval-worker alike (compose mounts the same secret into all of them).
var localAllServices = map[string]bool{"svc_local-dev": true, "svc_replicant-services": true}

// principalOf maps a subject string from the tokens file to a principal.
func principalOf(subject string) string {
	s := strings.TrimSpace(subject)
	if strings.HasPrefix(s, "system:serviceaccount:") {
		parts := strings.Split(s, ":")
		s = parts[len(parts)-1]
	}
	if !strings.HasPrefix(s, "svc_") {
		s = "svc_" + s
	}
	return s
}

func (a *Authenticator) loadServiceTokens() {
	if a.svcFile == "" {
		return
	}
	st, err := os.Stat(a.svcFile)
	if err != nil {
		return
	}
	a.mu.Lock()
	defer a.mu.Unlock()
	if a.svc != nil && st.ModTime().Equal(a.svcMtime) {
		return
	}
	b, err := os.ReadFile(a.svcFile)
	if err != nil {
		return
	}
	m := map[string]string{}
	var ym map[string]string
	if yaml.Unmarshal(b, &ym) == nil && len(ym) > 0 {
		// PLATFORM-02: YAML map `token: subject`
		for tok, sub := range ym {
			m[tok] = principalOf(sub)
		}
	} else {
		// bare token (single line): the local all-services principal
		if t := strings.TrimSpace(string(b)); t != "" && !strings.ContainsAny(t, " \n:") {
			m[t] = "svc_local-dev"
		}
	}
	a.svc, a.svcMtime = m, st.ModTime()
}

// ServiceCaller resolves a static service token.
func (a *Authenticator) ServiceCaller(tok string) (*Caller, bool) {
	a.loadServiceTokens()
	a.mu.Lock()
	p, ok := a.svc[tok]
	a.mu.Unlock()
	if !ok {
		return nil, false
	}
	rights := serviceRights[p]
	if localAllServices[p] {
		for _, r := range serviceRights {
			rights = append(rights, r...)
		}
	}
	return &Caller{Kind: "service", Service: p, Rights: rights}, true
}

// Has reports a service right.
func (c *Caller) Has(right string) bool {
	return c != nil && c.Kind == "service" && contains(c.Rights, right)
}

// bearer extracts the bearer token.
func bearer(h string) (string, error) {
	if h == "" {
		return "", errors.New("missing Authorization")
	}
	t, ok := strings.CutPrefix(h, "Bearer ")
	if !ok || strings.TrimSpace(t) == "" {
		return "", errors.New("not a bearer token")
	}
	return strings.TrimSpace(t), nil
}

// Authenticate resolves a request's caller: service token first (exact match), else a run token.
func (a *Authenticator) Authenticate(authz string) (*Caller, error) {
	tok, err := bearer(authz)
	if err != nil {
		return nil, unauth("%v", err)
	}
	if c, ok := a.ServiceCaller(tok); ok {
		return c, nil
	}
	rc, err := a.VerifyRunToken(tok)
	if err != nil {
		return nil, err
	}
	return &Caller{Kind: "run", Run: rc}, nil
}
