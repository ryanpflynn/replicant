package main

import (
	"encoding/json"
	"errors"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"sync"
	"time"

	"example.com/replicant/internal/runner/api"
	v1 "example.com/replicant/internal/runner/v1alpha1"
)

// Record is the local AgentRun record (local-runner.md L-2): the same AgentRun Go type plus the
// executor's own bookkeeping.
type Record struct {
	AgentRun v1.AgentRun `json:"agentrun"`
	Local    Local       `json:"local"`
}

// Local is runner-local executor state.
type Local struct {
	SpecDigest     string                      `json:"spec_digest"`
	IdempotencyKey string                      `json:"idempotency_key"`
	Grant          api.RunGrantClaims          `json:"grant"`
	Isolation      string                      `json:"isolation"`
	Runtime        string                      `json:"runtime"`
	AgentImage     string                      `json:"agent_image"`
	CLIImage       string                      `json:"cli_image"`
	CPUs           float64                     `json:"cpus"`
	MemoryBytes    int64                       `json:"memory_bytes"`
	WorkspaceBytes int64                       `json:"workspace_bytes"`
	SteeringLive   bool                        `json:"steering_live"`
	Repo           api.Repo                    `json:"repo"`
	EnvDefaults    map[string]string           `json:"env_defaults,omitempty"`
	Generation     int64                       `json:"generation"`
	Stage          string                      `json:"stage"` // executor progress
	PodCreateMark  bool                        `json:"pod_create_marked"`
	Containers     map[string]string           `json:"containers,omitempty"`
	PullError      string                      `json:"pull_error,omitempty"`
	PullContainer  string                      `json:"pull_container,omitempty"`
	Intent         string                      `json:"intent,omitempty"` // deadline | cancel
	IntentAt       *time.Time                  `json:"intent_at,omitempty"`
	StopSent       bool                        `json:"stop_sent,omitempty"`
	ControlAt      *time.Time                  `json:"control_at,omitempty"`
	LeaseAt        *time.Time                  `json:"lease_at,omitempty"`
	Disk           *api.DiskReport             `json:"disk,omitempty"`
	AgentStarted   bool                        `json:"agent_started"`
	GuardExit      *int                        `json:"guard_exit,omitempty"`
	PrepareExit    *int                        `json:"prepare_exit,omitempty"`
	AcceptFailure  string                      `json:"accept_failure,omitempty"`
	FailureMessage string                      `json:"failure_message,omitempty"`
	ManifestState  string                      `json:"manifest_state,omitempty"` // committed | partial | missing
	Notified       []string                    `json:"notified,omitempty"`
	Notes          []api.RunStatusNotification `json:"notes,omitempty"`
	NotifyAttempts int                         `json:"notify_attempts,omitempty"`
	NotifyNextAt   *time.Time                  `json:"notify_next_at,omitempty"`
	Revoked        bool                        `json:"revoked,omitempty"`
	RecordWritten  bool                        `json:"record_written,omitempty"`
	TerminalDone   bool                        `json:"terminal_done,omitempty"`
	ContainersGCed bool                        `json:"containers_gced,omitempty"`
	Messages       map[string]MsgState         `json:"messages,omitempty"`
	TraceParent    string                      `json:"traceparent,omitempty"`
}

// MsgState tracks steering message ids (the S3 inbox is authoritative).
type MsgState struct {
	Seq int `json:"seq"`
}

// Store persists records under $STATE/agentruns.
type Store struct {
	dir   string
	mu    sync.Mutex
	locks map[string]*sync.Mutex
	cache map[string]*Record
}

func NewStore(stateDir string) (*Store, error) {
	d := filepath.Join(stateDir, "agentruns")
	for _, p := range []string{d, filepath.Join(stateDir, "runs"), filepath.Join(stateDir, "leases")} {
		if err := os.MkdirAll(p, 0o755); err != nil {
			return nil, err
		}
	}
	return &Store{dir: d, locks: map[string]*sync.Mutex{}, cache: map[string]*Record{}}, nil
}

func (s *Store) lock(runID string) *sync.Mutex {
	s.mu.Lock()
	defer s.mu.Unlock()
	l, ok := s.locks[runID]
	if !ok {
		l = &sync.Mutex{}
		s.locks[runID] = l
	}
	return l
}

func (s *Store) path(runID string) string { return filepath.Join(s.dir, runID+".json") }

// ErrExists is returned by Create when the record exists.
var ErrExists = errors.New("exists")

// Create writes a new record with O_EXCL.
func (s *Store) Create(r *Record) error {
	b, err := json.MarshalIndent(r, "", " ")
	if err != nil {
		return err
	}
	f, err := os.OpenFile(s.path(r.AgentRun.Spec.RunID), os.O_CREATE|os.O_EXCL|os.O_WRONLY, 0o600)
	if err != nil {
		if os.IsExist(err) {
			return ErrExists
		}
		return err
	}
	if _, err := f.Write(b); err != nil {
		f.Close()
		return err
	}
	if err := f.Close(); err != nil {
		return err
	}
	s.mu.Lock()
	s.cache[r.AgentRun.Spec.RunID] = clone(r)
	s.mu.Unlock()
	return nil
}

// Get returns a copy of the record (nil if absent).
func (s *Store) Get(runID string) (*Record, error) {
	s.mu.Lock()
	if r, ok := s.cache[runID]; ok {
		s.mu.Unlock()
		return clone(r), nil
	}
	s.mu.Unlock()
	b, err := os.ReadFile(s.path(runID))
	if os.IsNotExist(err) {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}
	var r Record
	if err := json.Unmarshal(b, &r); err != nil {
		return nil, err
	}
	s.mu.Lock()
	s.cache[runID] = clone(&r)
	s.mu.Unlock()
	return &r, nil
}

// Update applies fn under the run lock and persists the result.
func (s *Store) Update(runID string, fn func(*Record) error) (*Record, error) {
	l := s.lock(runID)
	l.Lock()
	defer l.Unlock()
	r, err := s.Get(runID)
	if err != nil || r == nil {
		if err == nil {
			err = os.ErrNotExist
		}
		return nil, err
	}
	if err := fn(r); err != nil {
		return nil, err
	}
	b, err := json.MarshalIndent(r, "", " ")
	if err != nil {
		return nil, err
	}
	tmp := s.path(runID) + ".tmp"
	if err := os.WriteFile(tmp, b, 0o600); err != nil {
		return nil, err
	}
	if err := os.Rename(tmp, s.path(runID)); err != nil {
		return nil, err
	}
	s.mu.Lock()
	s.cache[runID] = clone(r)
	s.mu.Unlock()
	return r, nil
}

// List returns all records sorted by creation time.
func (s *Store) List() ([]*Record, error) {
	ents, err := os.ReadDir(s.dir)
	if err != nil {
		return nil, err
	}
	var out []*Record
	for _, e := range ents {
		n := e.Name()
		if !strings.HasSuffix(n, ".json") {
			continue
		}
		r, err := s.Get(strings.TrimSuffix(n, ".json"))
		if err == nil && r != nil {
			out = append(out, r)
		}
	}
	sort.Slice(out, func(i, j int) bool {
		ti, tj := out[i].AgentRun.CreationTimestamp.Time, out[j].AgentRun.CreationTimestamp.Time
		if ti.Equal(tj) {
			return out[i].AgentRun.Spec.RunID < out[j].AgentRun.Spec.RunID
		}
		return ti.Before(tj)
	})
	return out, nil
}

// Delete removes a record (GC after retention).
func (s *Store) Delete(runID string) error {
	s.mu.Lock()
	delete(s.cache, runID)
	s.mu.Unlock()
	return os.Remove(s.path(runID))
}

func clone(r *Record) *Record {
	b, _ := json.Marshal(r)
	var c Record
	_ = json.Unmarshal(b, &c)
	return &c
}
