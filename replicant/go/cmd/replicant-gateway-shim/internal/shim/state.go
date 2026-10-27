package shim

import (
	"encoding/json"
	"sync"
	"time"
)

// Stub selects a cassette or script (XD-FIX-R1-264).
type Stub struct {
	Root string `json:"root"`
	Set  string `json:"set"`
	Key  string `json:"key"`
}

// Policy is gateway-interface.openapi.yaml RunPolicy.
type Policy struct {
	TenantID        string            `json:"tenant_id"`
	JobID           string            `json:"job_id"`
	Role            string            `json:"role"`
	ReleaseBundleID string            `json:"release_bundle_id"`
	Models          []string          `json:"models"`
	BudgetUSD       float64           `json:"budget_usd"`
	MaxConcurrency  int               `json:"max_concurrency"`
	ExpiresAt       string            `json:"expires_at"`
	BudgetAccount   string            `json:"budget_account,omitempty"`
	Experiment      map[string]string `json:"experiment,omitempty"`
	Stub            *Stub             `json:"stub,omitempty"`
}

// TenantQuota is control-plane-api GatewayQuota.
type TenantQuota struct {
	TenantID              string  `json:"tenant_id"`
	MonthlyBudgetUSD      float64 `json:"monthly_budget_usd"`
	DailyBudgetUSD        float64 `json:"daily_budget_usd"`
	PlatformDailyUSDMax   float64 `json:"platform_daily_usd_max"`
	PlatformMonthlyUSDMax float64 `json:"platform_monthly_usd_max"`
	Version               int64   `json:"version"`
	AsOf                  string  `json:"as_of"`
}

// Usage is the token usage of one request.
type Usage struct {
	InputTokens      int64 `json:"input_tokens" yaml:"input_tokens"`
	OutputTokens     int64 `json:"output_tokens" yaml:"output_tokens"`
	CacheReadTokens  int64 `json:"cache_read_tokens,omitempty" yaml:"cache_read_tokens"`
	CacheWriteTokens int64 `json:"cache_write_tokens,omitempty" yaml:"cache_write_tokens"`
	ReasoningTokens  int64 `json:"reasoning_tokens,omitempty" yaml:"reasoning_tokens"`
}

// Level is one budget level of an admission (gateway.md §6.2): a counter key and its limit.
type Level struct {
	Name  string // run, job, tenant_period, account, platform, ...
	Key   string // e.g. fgw:run:<run_id>
	Limit *int64 // nil = no limit configured at this level
}

type counter struct {
	limit    *int64
	spent    int64
	reserved int64
}

type reservation struct {
	keys     []string
	estimate int64
	at       time.Time
}

// MemState is the v0 in-process state backend (`--state=local`, gateway.md Risks "break-glass local mode"):
// run policies, job budgets, budget accounts, counters, revocations, in-flight counts and script cursors.
// Single replica only; contents are lost on restart (the worker re-registers policies on 503 run_policy_missing).
type MemState struct {
	mu           sync.Mutex
	policies     map[string]policyEntry
	jobLimits    map[string]int64
	jobAccounts  map[string]JobAccount
	accountCaps  map[string]int64
	counters     map[string]*counter
	reservations map[string]reservation
	revokedRuns  map[string]time.Time // value = expiry
	revokedJobs  map[string]time.Time
	inflight     map[string]int
	scripts      map[string]*scriptCursor
}

type policyEntry struct {
	p     Policy
	raw   []byte
	until time.Time
}

// JobAccount is the JobBudgetAccount body.
type JobAccount struct {
	Account        string  `json:"account"`
	CapUSD         float64 `json:"cap_usd"`
	EvaluationMode string  `json:"evaluation_mode,omitempty"`
}

// NewMemState returns an empty store.
func NewMemState() *MemState {
	return &MemState{
		policies: map[string]policyEntry{}, jobLimits: map[string]int64{}, jobAccounts: map[string]JobAccount{},
		accountCaps: map[string]int64{}, counters: map[string]*counter{}, reservations: map[string]reservation{},
		revokedRuns: map[string]time.Time{}, revokedJobs: map[string]time.Time{}, inflight: map[string]int{},
		scripts: map[string]*scriptCursor{},
	}
}

// PutPolicy stores a policy idempotently. Returns (stored, conflict).
func (s *MemState) PutPolicy(runID string, p Policy) (Policy, bool) {
	raw, _ := json.Marshal(p)
	s.mu.Lock()
	defer s.mu.Unlock()
	now := time.Now()
	if e, ok := s.policies[runID]; ok && now.Before(e.until) {
		if string(e.raw) == string(raw) {
			return e.p, false
		}
		return e.p, true
	}
	until, err := time.Parse(time.RFC3339, p.ExpiresAt)
	if err != nil {
		until = now.Add(24 * time.Hour)
	}
	s.policies[runID] = policyEntry{p: p, raw: raw, until: until}
	return p, false
}

// GetPolicy returns the live policy of a run.
func (s *MemState) GetPolicy(runID string) (Policy, bool) {
	s.mu.Lock()
	defer s.mu.Unlock()
	e, ok := s.policies[runID]
	if !ok || time.Now().After(e.until) {
		return Policy{}, false
	}
	return e.p, true
}

// SetJobLimit sets fgw:job:<job_id>:limit.
func (s *MemState) SetJobLimit(jobID string, micros int64) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.jobLimits[jobID] = micros
	s.counterLocked("fgw:job:" + jobID).limit = &micros
}

// JobLimit returns the registered job limit.
func (s *MemState) JobLimit(jobID string) (int64, bool) {
	s.mu.Lock()
	defer s.mu.Unlock()
	v, ok := s.jobLimits[jobID]
	return v, ok
}

// BindJobAccount implements PUT /internal/v1/budgets/jobs/{job_id}. Returns a conflict code or "".
func (s *MemState) BindJobAccount(jobID string, a JobAccount) string {
	s.mu.Lock()
	defer s.mu.Unlock()
	if cur, ok := s.jobAccounts[jobID]; ok {
		if cur == a {
			return ""
		}
		return "job_bound_elsewhere"
	}
	cap := USDToMicros(a.CapUSD)
	if c, ok := s.accountCaps[a.Account]; ok && c != cap {
		return "account_cap_conflict"
	}
	s.accountCaps[a.Account] = cap
	s.counterLocked("fgw:acct:" + a.Account).limit = &cap
	s.jobAccounts[jobID] = a
	return ""
}

// JobAccountOf returns the bound account of a job.
func (s *MemState) JobAccountOf(jobID string) (JobAccount, bool) {
	s.mu.Lock()
	defer s.mu.Unlock()
	a, ok := s.jobAccounts[jobID]
	return a, ok
}

func (s *MemState) counterLocked(key string) *counter {
	c, ok := s.counters[key]
	if !ok {
		c = &counter{}
		s.counters[key] = c
	}
	return c
}

// Admit is the admission script of gateway.md §6.2: deny when spent + reserved >= limit at the first
// blocked level; otherwise reserve the estimate at every level.
func (s *MemState) Admit(levels []Level, estimate int64, requestID string) (bool, string) {
	s.mu.Lock()
	defer s.mu.Unlock()
	for _, l := range levels {
		c := s.counterLocked(l.Key)
		limit := c.limit
		if l.Limit != nil {
			limit = l.Limit
			c.limit = l.Limit
		}
		if limit == nil {
			continue
		}
		if c.spent+c.reserved >= *limit {
			return false, l.Name
		}
	}
	keys := make([]string, 0, len(levels))
	for _, l := range levels {
		s.counterLocked(l.Key).reserved += estimate
		keys = append(keys, l.Key)
	}
	s.reservations[requestID] = reservation{keys: keys, estimate: estimate, at: time.Now()}
	return true, "ok"
}

// Settle releases the reservation and charges the actual cost at every level.
func (s *MemState) Settle(requestID string, actual int64) {
	s.mu.Lock()
	defer s.mu.Unlock()
	r, ok := s.reservations[requestID]
	if !ok {
		return
	}
	delete(s.reservations, requestID)
	for _, k := range r.keys {
		c := s.counterLocked(k)
		c.reserved -= r.estimate
		if c.reserved < 0 {
			c.reserved = 0
		}
		c.spent += actual
	}
}

// Janitor charges reservations older than maxAge at their estimate (crashed request).
func (s *MemState) Janitor(maxAge time.Duration) int {
	s.mu.Lock()
	var stale []string
	for id, r := range s.reservations {
		if time.Since(r.at) > maxAge {
			stale = append(stale, id)
		}
	}
	s.mu.Unlock()
	for _, id := range stale {
		s.mu.Lock()
		est := s.reservations[id].estimate
		s.mu.Unlock()
		s.Settle(id, est)
	}
	return len(stale)
}

// CounterState returns (limit, spent, reserved) of a key.
func (s *MemState) CounterState(key string) (*int64, int64, int64) {
	s.mu.Lock()
	defer s.mu.Unlock()
	c := s.counterLocked(key)
	return c.limit, c.spent, c.reserved
}

// Revoke adds run ids / job ids to the revoked sets.
func (s *MemState) Revoke(runIDs, jobIDs []string, ttl time.Duration) {
	s.mu.Lock()
	defer s.mu.Unlock()
	until := time.Now().Add(ttl)
	for _, r := range runIDs {
		if cur, ok := s.revokedRuns[r]; !ok || cur.Before(until) {
			s.revokedRuns[r] = until
		}
	}
	for _, j := range jobIDs {
		if cur, ok := s.revokedJobs[j]; !ok || cur.Before(until) {
			s.revokedJobs[j] = until
		}
	}
}

// IsRevoked reports whether a run or its job is revoked.
func (s *MemState) IsRevoked(runID, jobID string) bool {
	s.mu.Lock()
	defer s.mu.Unlock()
	now := time.Now()
	if t, ok := s.revokedRuns[runID]; ok && now.Before(t) {
		return true
	}
	if t, ok := s.revokedJobs[jobID]; ok && now.Before(t) {
		return true
	}
	return false
}

// AcquireInflight increments the run's in-flight count if below max.
func (s *MemState) AcquireInflight(key string, max int) bool {
	s.mu.Lock()
	defer s.mu.Unlock()
	if max > 0 && s.inflight[key] >= max {
		return false
	}
	s.inflight[key]++
	return true
}

// ReleaseInflight decrements the in-flight count.
func (s *MemState) ReleaseInflight(key string) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.inflight[key] > 0 {
		s.inflight[key]--
	}
}

// scriptCursor tracks a run's position in its script (see script.go).
type scriptCursor struct {
	next   int
	served map[int]int // message count of the request -> turn index served for it
}

// ScriptCursor returns the cursor for a run (created on first use) under the state lock held by fn.
func (s *MemState) WithScriptCursor(key string, fn func(c *scriptCursor)) {
	s.mu.Lock()
	defer s.mu.Unlock()
	c, ok := s.scripts[key]
	if !ok {
		c = &scriptCursor{served: map[int]int{}}
		s.scripts[key] = c
	}
	fn(c)
}
