package main

import (
	"crypto/rand"
	"crypto/sha256"
	"encoding/base32"
	"encoding/hex"
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"time"

	v1 "example.com/replicant/internal/runner/v1alpha1"
)

// Canary credential (RC-RUN-20, "the trigger WS-02 defines for SecurityViolation ... for example a canary credential
// read"): every agent container gets a run-specific decoy AWS credentials file at ~/.aws/credentials. The decoy key
// id never exists in the object store; sbx-edge recognises it in the SigV4 Authorization header of any request to
// s3.sbx.local and reports it here (internal location only, the sandbox-facing runner-sandbox host refuses this
// path), and runner-local ends the run exactly like a `security` cancel: Failed/SecurityViolation, non-retryable,
// on the revocation feed.

func canaryKey(runID string) string {
	h := sha256.Sum256([]byte("replicant.canary.v1:" + runID))
	return "AKIAFC" + strings.ToUpper(base32.StdEncoding.EncodeToString(h[:]))[:14]
}

func writeCanary(stateDir, runID string) error {
	dir := filepath.Join(stateDir, "runs", runID, "canary")
	if err := os.MkdirAll(dir, 0o755); err != nil {
		return err
	}
	sk := make([]byte, 30)
	_, _ = rand.Read(sk)
	body := "[default]\naws_access_key_id = " + canaryKey(runID) + "\naws_secret_access_key = " + hex.EncodeToString(sk)[:40] + "\n"
	return os.WriteFile(filepath.Join(dir, "credentials"), []byte(body), 0o644)
}

// canaryTripped: POST /sandbox/v1/security/canary-tripped from sbx-edge (header X-Replicant-Canary-Key).
func (s *Server) canaryTripped(w http.ResponseWriter, r *http.Request) {
	key := r.Header.Get("X-Replicant-Canary-Key")
	recs, _ := s.Store.List()
	for _, rec := range recs {
		if rec.AgentRun.Status.Phase.IsTerminal() || canaryKey(rec.AgentRun.Spec.RunID) != key {
			continue
		}
		id := rec.AgentRun.Spec.RunID
		rr, err := s.Store.Update(id, func(x *Record) error {
			if !x.AgentRun.Spec.CancelRequested {
				x.AgentRun.Spec.CancelRequested = true
				x.AgentRun.Spec.CancelReason = v1.CancelReason("security")
				x.AgentRun.Generation++
				x.Local.Generation = x.AgentRun.Generation
			}
			return nil
		})
		if err == nil {
			if err := s.Inbox.WriteControl(r.Context(), rr.AgentRun.Spec.Artifacts.ArtifactsPrefix, rr.AgentRun.Spec.TenantID, "security"); err == nil {
				now := time.Now()
				_, _ = s.Store.Update(id, func(x *Record) error { x.Local.ControlAt = &now; return nil })
			}
		}
		s.Log.Warn("canary credential used: security stop", "run_id", id, "job_id", rec.AgentRun.Spec.JobID,
			"source_ip", r.Header.Get("X-Replicant-Source-IP"), "path", truncate(r.Header.Get("X-Replicant-Original-URI"), 200))
		s.kick(id)
		break
	}
	w.Header().Set("Content-Type", "application/xml")
	w.WriteHeader(http.StatusForbidden)
	_, _ = w.Write([]byte(`<?xml version="1.0" encoding="UTF-8"?><Error><Code>AccessDenied</Code><Message>Access Denied</Message></Error>`))
}

// testHold (RUNNER_TEST_HOOKS=true only, never in shared environments): runs whose logical activity ID starts with
// `holdpending` stay Pending (no pod group) until cancelled; RC-CAN-01's "runner paused via test hook".
func (s *Server) testHold(rec *Record) bool {
	return os.Getenv("RUNNER_TEST_HOOKS") == "true" && strings.HasPrefix(rec.AgentRun.Spec.LogicalActivityID, "holdpending")
}
