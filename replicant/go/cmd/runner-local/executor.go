package main

import (
	"context"
	"os"
	"path/filepath"
	"sync"
	"time"

	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"

	"example.com/replicant/internal/runner/controller"
	"example.com/replicant/internal/runner/sandbox"
	v1 "example.com/replicant/internal/runner/v1alpha1"
)

// Container names (C-IDS §8) in pod-group order (local-runner.md §1.2).
const (
	cPause      = "pause"
	cVolumeInit = "volume-init" // local-only fsGroup emulation, removed after it exits (DEVIATIONS)
	cGuard      = controller.CWorkspaceGuard
	cAgentd     = controller.CAgentd
	cWatchdog   = controller.CWatchdog
	cPrepare    = controller.CPrepare
	cAgent      = controller.CAgent
	cUploader   = controller.CUploader
)

var volumeNames = []string{"workspace", "home", "tools", "managed", "inputs", "out", "spool"}
var tmpfsVolumes = map[string]string{"ipc": "size=1m,mode=1777", "uploader-ipc": "size=64k,mode=1777", "agentd-state": "size=1m,mode=1777"}

const termPath = "/replicant/out/.termination/agent"

// Executor runs one goroutine per non-terminal run.
type Executor struct {
	s       *Server
	mu      sync.Mutex
	running map[string]bool
}

func (s *Server) runExecutor(ctx context.Context) {
	ex := &Executor{s: s, running: map[string]bool{}}
	start := func(id string) {
		ex.mu.Lock()
		defer ex.mu.Unlock()
		if ex.running[id] {
			return
		}
		ex.running[id] = true
		go func() {
			defer func() {
				ex.mu.Lock()
				delete(ex.running, id)
				ex.mu.Unlock()
			}()
			ex.loop(ctx, id)
		}()
	}
	// Restart recovery: resume every record that is not fully done.
	if recs, err := s.Store.List(); err == nil {
		for _, r := range recs {
			if !r.Local.ContainersGCed {
				start(r.AgentRun.Spec.RunID)
			}
		}
	}
	gc := time.NewTicker(time.Minute)
	defer gc.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case id := <-s.wake:
			start(id)
		case <-gc.C:
			s.gcRecords()
			if recs, err := s.Store.List(); err == nil {
				for _, r := range recs {
					if !r.Local.ContainersGCed {
						start(r.AgentRun.Spec.RunID)
					}
				}
			}
		}
	}
}

func (ex *Executor) loop(ctx context.Context, runID string) {
	log := ex.s.Log.With("run_id", runID)
	for {
		done, err := ex.s.reconcile(ctx, runID)
		if err != nil {
			log.Warn("reconcile error", "err", err)
		}
		if done {
			return
		}
		select {
		case <-ctx.Done():
			return
		case <-time.After(time.Second):
		}
	}
}

// reconcile performs one idempotent pass for a run; it returns true when the run needs no further passes.
func (s *Server) reconcile(ctx context.Context, runID string) (bool, error) {
	rec, err := s.Store.Get(runID)
	if err != nil || rec == nil {
		return true, err
	}
	if rec.AgentRun.Status.Phase.IsTerminal() {
		return s.reconcileTerminal(ctx, rec)
	}
	now := time.Now()
	ar := &rec.AgentRun
	// Accepted + credentials (controller.md §5).
	if rec.Local.Stage == "accepted" {
		if err := s.prepareRunState(rec); err != nil {
			return false, s.failNow(rec, v1.ReasonCredentialIssueFailed, err.Error())
		}
		rec, err = s.Store.Update(runID, func(rr *Record) error {
			setCond(&rr.AgentRun, v1.CondAccepted, metav1.ConditionTrue, "Accepted", "", now)
			setCond(&rr.AgentRun, v1.CondCredentialsIssued, metav1.ConditionTrue, "Issued", "", now)
			rr.AgentRun.Status.AcceptedAt = mt(now)
			rr.Local.Stage = "credentials"
			return nil
		})
		if err != nil {
			return false, err
		}
		s.queueNotification(rec, string(v1.PhasePending), "")
	}
	ar = &rec.AgentRun
	if !rec.Local.PodCreateMark && s.testHold(rec) {
		return s.deriveAndApply(ctx, rec)
	}
	// Pod creation (marker first, at-most-once).
	if !rec.Local.PodCreateMark && !ar.Spec.CancelRequested {
		for _, img := range []string{s.Cfg.PauseImage, s.Cfg.SidecarsImage, rec.Local.CLIImage, rec.Local.AgentImage} {
			if err := s.ensureImage(ctx, img); err != nil {
				_, _ = s.Store.Update(runID, func(rr *Record) error { rr.Local.PullError, rr.Local.PullContainer = err.Error(), cPrepare; return nil })
				rec, _ = s.Store.Get(runID)
				return s.deriveAndApply(ctx, rec)
			}
		}
		rec, err = s.Store.Update(runID, func(rr *Record) error {
			rr.Local.PodCreateMark, rr.Local.PullError = true, ""
			setCond(&rr.AgentRun, v1.CondScheduled, metav1.ConditionUnknown, "Waiting", "", now)
			return nil
		})
		if err != nil {
			return false, err
		}
		if err := s.createPodGroup(ctx, rec); err != nil {
			s.Log.Warn("pod group creation failed", "run_id", runID, "err", err)
			_, _ = s.Store.Update(runID, func(rr *Record) error { rr.Local.FailureMessage = "pod create: " + err.Error(); return nil })
			return false, s.failNow(rec, v1.ReasonControllerError, "sandbox creation failed: "+truncate(err.Error(), 500))
		}
		rec, _ = s.Store.Get(runID)
	}
	if rec.Local.PodCreateMark {
		if err := s.advancePodGroup(ctx, rec); err != nil {
			s.Log.Warn("advance failed", "run_id", runID, "err", err)
		}
		rec, _ = s.Store.Get(runID)
	}
	return s.deriveAndApply(ctx, rec)
}

// prepareRunState mints the per-audience tokens and renders bootstrap.json into the state directory.
func (s *Server) prepareRunState(r *Record) error {
	runID := r.AgentRun.Spec.RunID
	set, err := signSet(s.Signer, s.Cfg.Issuer, r.AgentRun.Spec.RunGrant, &r.Local.Grant, r.Local.SteeringLive, time.Now(), nil)
	if err != nil {
		return err
	}
	if err := writeTokenFiles(s.Cfg.StateDir, runID, set); err != nil {
		return err
	}
	cfgDir := filepath.Join(s.Cfg.StateDir, "runs", runID, "config")
	if err := os.MkdirAll(cfgDir, 0o755); err != nil {
		return err
	}
	if err := writeCanary(s.Cfg.StateDir, runID); err != nil {
		return err
	}
	b := s.bootstrap(r)
	return sandbox.WriteJSONAtomic(filepath.Join(cfgDir, "bootstrap.json"), b, 0o644)
}

func (s *Server) failNow(r *Record, reason v1.FailureReason, msg string) error {
	_, err := s.Store.Update(r.AgentRun.Spec.RunID, func(rr *Record) error {
		rr.Local.AcceptFailure = string(reason)
		rr.Local.FailureMessage = msg
		return nil
	})
	return err
}
