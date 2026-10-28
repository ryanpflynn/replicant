package shim

import (
	"context"
	"encoding/json"
	"errors"
	"log/slog"
	"net/http"
	"strings"
	"time"

	"example.com/replicant/cmd/replicant-gateway-shim/internal/usage"
)

// UsageSink is the subset of usage.Log the server needs.
type UsageSink interface {
	Append(usage.Event) error
	Healthy() bool
}

// Server is replicant-gateway-shim's HTTP surface (gateway-interface.openapi.yaml).
type Server struct {
	Cfg     *Config
	State   *MemState
	Auth    *Authenticator
	Quotas  *QuotaClient
	Backend Backend
	Usage   UsageSink
	Feeds   *Feeds
	Log     *slog.Logger
	Now     func() time.Time
}

// BudgetCounter is the gateway-interface BudgetCounter.
type BudgetCounter struct {
	LimitUSD    *float64 `json:"limit_usd"`
	SpentUSD    float64  `json:"spent_usd"`
	ReservedUSD float64  `json:"reserved_usd"`
	Period      string   `json:"period,omitempty"`
}

// BudgetState is the gateway-interface BudgetState.
type BudgetState struct {
	Run          BudgetCounter `json:"run"`
	Job          BudgetCounter `json:"job"`
	TenantPeriod BudgetCounter `json:"tenant_period"`
}

func (s *Server) now() time.Time {
	if s.Now != nil {
		return s.Now()
	}
	return time.Now()
}

// Handler returns the router.
func (s *Server) Handler() http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("GET /healthz", func(w http.ResponseWriter, r *http.Request) { w.WriteHeader(200); _, _ = w.Write([]byte("ok\n")) })
	mux.HandleFunc("GET /readyz", s.readyz)
	mux.HandleFunc("GET /v1/budget", s.getBudget)
	mux.HandleFunc("POST /v1/runs/self:close", s.selfClose)
	mux.HandleFunc("POST /internal/v1/revocations", s.revocations)
	mux.HandleFunc("PUT /internal/v1/runs/{run_id}/policy", s.putPolicy)
	mux.HandleFunc("GET /internal/v1/runs/{run_id}/policy", s.getPolicy)
	mux.HandleFunc("PUT /internal/v1/jobs/{job_id}/budget", s.putJobBudget)
	mux.HandleFunc("PUT /internal/v1/budgets/jobs/{job_id}", s.putJobAccount)
	mux.HandleFunc("GET /internal/v1/models", s.models)
	for name, rt := range s.Cfg.File.Routes {
		rt := rt
		_ = name
		prefix := strings.TrimRight(rt.Prefix, "/")
		switch rt.Protocol {
		case "anthropic_messages":
			mux.HandleFunc("POST "+prefix+"/v1/messages", s.dataPlane("anthropic_messages", rt))
			mux.HandleFunc("POST "+prefix+"/v1/messages/count_tokens", s.dataPlane("anthropic_count_tokens", rt))
		case "openai_responses":
			mux.HandleFunc("POST "+prefix+"/v1/responses", s.dataPlane("openai_responses", rt))
		case "embeddings":
			mux.HandleFunc("POST "+prefix+"/v1/embeddings", s.dataPlane("embeddings", rt))
		}
	}
	return mux
}

func (s *Server) readyz(w http.ResponseWriter, r *http.Request) {
	ready := len(s.Cfg.Prices) > 0 && (s.Usage == nil || s.Usage.Healthy())
	body := map[string]any{"ready": ready, "backend": s.Backend.Name(), "state": s.Cfg.StateMode, "keys": s.Auth.KeyStatus()}
	j, _ := json.Marshal(body)
	w.Header().Set("Content-Type", "application/json")
	if !ready {
		w.WriteHeader(503)
	}
	_, _ = w.Write(j)
}

func jsonOut(w http.ResponseWriter, status int, v any) {
	j, _ := json.Marshal(v)
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_, _ = w.Write(j)
}

func (s *Server) authErr(w http.ResponseWriter, protocol string, err error) {
	var ae *AuthError
	if errors.As(err, &ae) {
		writeError(w, protocol, ae.Status, ae.Code, ae.Msg, nil)
		return
	}
	writeError(w, protocol, 401, "unauthenticated", err.Error(), nil)
}

// RunBackground starts the janitor (reservations older than request timeout + 60 s).
func (s *Server) RunBackground(ctx context.Context) {
	go func() {
		t := time.NewTicker(60 * time.Second)
		defer t.Stop()
		for {
			select {
			case <-ctx.Done():
				return
			case <-t.C:
				if n := s.State.Janitor(11 * time.Minute); n > 0 && s.Log != nil {
					s.Log.Warn("janitor charged stale reservations", "count", n)
				}
			}
		}
	}()
}
