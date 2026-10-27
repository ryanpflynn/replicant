package main

import (
	"encoding/json"
	"io"
	"net/http"
	"strconv"
	"strings"
	"time"

	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"

	"example.com/replicant/internal/runner/api"
	"example.com/replicant/internal/runner/authz"
	"example.com/replicant/internal/runner/httpx"
	"example.com/replicant/internal/runner/ids"
	v1 "example.com/replicant/internal/runner/v1alpha1"
)

// SandboxHandler serves the sandbox listener (:8490): per-run credentials, steering inbox, heartbeat.
func (s *Server) SandboxHandler() http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("GET /healthz", func(w http.ResponseWriter, r *http.Request) { w.WriteHeader(200) })
	mux.HandleFunc("GET /.well-known/jwks.json", s.jwks)
	mux.HandleFunc("POST /sandbox/v1/token:refresh", s.sbx(s.refresh))
	mux.HandleFunc("GET /sandbox/v1/credentials/aws", s.sbx(s.awsCreds))
	mux.HandleFunc("GET /sandbox/v1/messages", s.sbx(s.pollMessages))
	mux.HandleFunc("POST /sandbox/v1/messages/", s.sbx(s.ackMessage))
	mux.HandleFunc("POST /sandbox/v1/heartbeat", s.sbx(s.heartbeat))
	mux.HandleFunc("POST /sandbox/v1/security/canary-tripped", s.canaryTripped)                 // sbx-edge only (RC-RUN-20)
	mux.HandleFunc("/v1/", func(w http.ResponseWriter, r *http.Request) { w.WriteHeader(404) }) // RC-SBX-05
	return s.logMW("sandbox", mux)
}

type sbxHandler func(w http.ResponseWriter, r *http.Request, v *authz.Verified, rec *Record)

// sbx applies the C-RUNNER verifier obligations (runner-service.md §1.2) for every sandbox endpoint.
func (s *Server) sbx(h sbxHandler) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		tok := httpx.Bearer(r)
		if tok == "" {
			api.WriteProblem(w, api.NewProblem("unauthenticated", "missing run token"))
			return
		}
		v, err := s.Tokens.Verify(r.Context(), tok)
		if err != nil {
			api.WriteProblem(w, api.NewProblem("unauthenticated", err.Error()))
			return
		}
		rec, _ := s.Store.Get(v.Claims.RunID)
		if rec == nil || rec.AgentRun.Spec.TenantID != v.Claims.TenantID {
			api.WriteProblem(w, api.NewProblem("unauthenticated", "unknown run"))
			return
		}
		if rec.AgentRun.Status.Phase.IsTerminal() {
			fin := rec.AgentRun.Status.FinishedAt
			if fin == nil || time.Since(fin.Time) > time.Duration(rec.AgentRun.Spec.Timeouts.FinalizeSeconds)*time.Second {
				api.WriteProblem(w, api.NewProblem("run_not_active", "run is terminal"))
				return
			}
		}
		h(w, r, v, rec)
	}
}

func (s *Server) refresh(w http.ResponseWriter, r *http.Request, v *authz.Verified, rec *Record) {
	ar := &rec.AgentRun
	if ar.Status.Phase.IsTerminal() {
		api.WriteProblem(w, api.NewProblem("run_not_active", "run is terminal"))
		return
	}
	if rec.Local.LeaseAt != nil && time.Since(*rec.Local.LeaseAt) > time.Duration(s.Cfg.LeaseSeconds)*time.Second {
		api.WriteProblem(w, api.NewProblem("run_not_active", "run lease expired"))
		return
	}
	var only []string
	if ar.Spec.CancelRequested {
		if ar.Spec.CancelReason == v1.CancelReasonSecurity {
			api.WriteProblem(w, api.NewProblem("run_not_active", "cancelled for security"))
			return
		}
		only = []string{api.AudSandbox} // partial upload only (R1-039)
	}
	set, err := signSet(s.Signer, s.Cfg.Issuer, ar.Spec.RunGrant, &v.Grant, rec.Local.SteeringLive, time.Now(), only)
	if err != nil {
		api.WriteProblem(w, api.NewProblem("run_not_active", err.Error()))
		return
	}
	api.WriteJSON(w, 200, api.RunTokenResponse{Tokens: set})
}

func (s *Server) awsCreds(w http.ResponseWriter, r *http.Request, v *authz.Verified, rec *Record) {
	if !v.HasScope("credentials:aws") {
		api.WriteProblem(w, api.NewProblem("forbidden", "scope credentials:aws required"))
		return
	}
	pre := api.ArtifactPrefixes{Artifacts: rec.AgentRun.Spec.Artifacts.ArtifactsPrefix, Transcripts: rec.AgentRun.Spec.Artifacts.TranscriptsPrefix, Session: rec.AgentRun.Spec.Artifacts.SessionPrefix}
	cr, err := s.Creds.For(r.Context(), s.Cfg.Org, s.Cfg.Env, rec.AgentRun.Spec.RunID, pre)
	if err != nil {
		p := api.NewProblem("runner_unavailable", "credentials: "+err.Error())
		p.RetryAfterSeconds = 5
		api.WriteProblem(w, p)
		return
	}
	api.WriteJSON(w, 200, cr)
}

func (s *Server) pollMessages(w http.ResponseWriter, r *http.Request, v *authz.Verified, rec *Record) {
	if !v.HasScope("inbox:read") {
		api.WriteJSON(w, 200, api.InboxPoll{Messages: []api.InboxMessage{}})
		return
	}
	after, _ := strconv.Atoi(r.URL.Query().Get("after_seq"))
	wait := 20
	if ws := r.URL.Query().Get("wait_seconds"); ws != "" {
		wait, _ = strconv.Atoi(ws)
	}
	if wait < 0 {
		wait = 0
	}
	if wait > 30 {
		wait = 30
	}
	msgs, err := s.Inbox.Poll(r.Context(), rec.AgentRun.Spec.Artifacts.ArtifactsPrefix, after, time.Duration(wait)*time.Second)
	if err != nil {
		api.WriteProblem(w, api.NewProblem("runner_unavailable", err.Error()))
		return
	}
	api.WriteJSON(w, 200, api.InboxPoll{Messages: msgs})
}

func (s *Server) ackMessage(w http.ResponseWriter, r *http.Request, v *authz.Verified, rec *Record) {
	rest := strings.TrimPrefix(r.URL.Path, "/sandbox/v1/messages/")
	id, action, _ := strings.Cut(rest, ":")
	if action != "ack" || !ids.MessageIDRe.MatchString(id) {
		api.WriteProblem(w, api.NewProblem("run_not_found", "no such message route"))
		return
	}
	var a api.AckRequest
	b, _ := io.ReadAll(io.LimitReader(r.Body, 8192))
	if json.Unmarshal(b, &a) != nil || (a.State != "delivered" && a.State != "rejected") {
		badRequest(w, "state", "enum", "state must be delivered or rejected")
		return
	}
	if err := s.Inbox.Ack(r.Context(), rec.AgentRun.Spec.Artifacts.ArtifactsPrefix, rec.AgentRun.Spec.TenantID, id, a); err != nil {
		api.WriteProblem(w, api.NewProblem("runner_unavailable", err.Error()))
		return
	}
	w.WriteHeader(204)
}

func (s *Server) heartbeat(w http.ResponseWriter, r *http.Request, v *authz.Verified, rec *Record) {
	var hb api.HeartbeatRequest
	b, _ := io.ReadAll(io.LimitReader(r.Body, 8192))
	if len(b) > 0 {
		_ = json.Unmarshal(b, &hb)
	}
	now := time.Now()
	rec, err := s.Store.Update(v.Claims.RunID, func(rr *Record) error {
		rr.Local.LeaseAt = &now
		if hb.Disk != nil {
			rr.Local.Disk = hb.Disk
			if !rr.AgentRun.Status.Phase.IsTerminal() {
				switch hb.Disk.State {
				case "exceeded":
					setCond(&rr.AgentRun, v1.CondDiskPressure, metav1.ConditionTrue, "Exceeded", "", now)
				case "warn":
					setCond(&rr.AgentRun, v1.CondDiskPressure, metav1.ConditionTrue, "Warning", "", now)
				default:
					setCond(&rr.AgentRun, v1.CondDiskPressure, metav1.ConditionFalse, "Ok", "", now)
				}
			}
		}
		return nil
	})
	if err != nil {
		api.WriteProblem(w, api.NewProblem("internal", err.Error()))
		return
	}
	resp := api.HeartbeatResponse{Continue: true}
	reason, err := s.Inbox.ReadControl(r.Context(), rec.AgentRun.Spec.Artifacts.ArtifactsPrefix)
	if err != nil || reason == "" {
		if rec.AgentRun.Spec.CancelRequested {
			reason = string(rec.AgentRun.Spec.CancelReason)
		} else if rec.Local.Intent == "deadline" {
			reason = "deadline"
		}
	}
	if reason != "" {
		resp.CancelReason = &reason
	}
	if rec.AgentRun.Status.Phase.IsTerminal() || s.Feed.IsRevoked(rec.AgentRun.Spec.RunID) {
		resp.Continue = false
		if resp.CancelReason == nil {
			rv := "revoked"
			resp.CancelReason = &rv
		}
	}
	api.WriteJSON(w, 200, resp)
}
