package main

import (
	"context"
	"encoding/json"
	"os"
	"path/filepath"
	"sync"
	"time"

	"k8s.io/apimachinery/pkg/api/meta"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"

	"example.com/replicant/internal/runner/s3x"
	v1 "example.com/replicant/internal/runner/v1alpha1"
)

// terminateIfNeeded performs the graceful delete of controller.md §6.3 (cancel or deadline).
func (s *Server) terminateIfNeeded(ctx context.Context, r *Record) error {
	ar := &r.AgentRun
	if r.Local.StopSent {
		// After agent and uploader exited, stop the sidecars.
		agent, up := s.inspect(ctx, r, cAgent), s.inspect(ctx, r, cUploader)
		if (agent == nil || exited(agent)) && (up == nil || exited(up)) {
			go s.stopContainers(context.Background(), r, []string{cAgentd, cWatchdog, cPause}, 5)
		}
		return nil
	}
	if ar.Spec.CancelRequested && ar.Spec.CancelReason != v1.CancelReasonSecurity && r.Local.Intent != "deadline" {
		// 2a: wait for the reason to reach agentd (a lease renewal after the control write, or 35 s).
		if r.Local.ControlAt != nil && time.Since(*r.Local.ControlAt) < 35*time.Second &&
			!(r.Local.LeaseAt != nil && r.Local.LeaseAt.After(*r.Local.ControlAt)) && s.inspect(ctx, r, cAgent) != nil {
			return nil
		}
	}
	grace := int(ar.Spec.Timeouts.FinalizeSeconds) + s.Cfg.StopGraceExtra
	_, err := s.Store.Update(ar.Spec.RunID, func(rr *Record) error {
		rr.Local.StopSent = true
		if rr.Local.Intent == "" {
			rr.Local.Intent = "cancel"
			now := time.Now()
			rr.Local.IntentAt = &now
		}
		return nil
	})
	if err != nil {
		return err
	}
	go s.stopContainers(context.Background(), r, []string{cAgent, cUploader, cPrepare, cGuard}, grace)
	return nil
}

func (s *Server) stopContainers(ctx context.Context, r *Record, names []string, grace int) {
	var wg sync.WaitGroup
	for _, c := range names {
		wg.Add(1)
		go func(c string) {
			defer wg.Done()
			_ = s.Engine.Stop(ctx, cname(r, c), grace)
		}(c)
	}
	wg.Wait()
}

// reconcileTerminal: notify, revoke, delete credentials, agentrun_record, container GC (controller.md §7.1).
func (s *Server) reconcileTerminal(ctx context.Context, r *Record) (bool, error) {
	ar := &r.AgentRun
	runID := ar.Spec.RunID
	s.deliverNotifications(ctx, runID)
	if !r.Local.TerminalDone {
		reason := string(ar.Status.Phase)
		if c := meta.FindStatusCondition(ar.Status.Conditions, v1.CondSucceeded); c != nil && ar.Status.Phase != v1.PhaseSucceeded {
			reason = c.Reason
		}
		s.Feed.Add(ctx, runID, ar.Spec.JobID, reason)
		removeTokenFiles(s.Cfg.StateDir, runID)
		s.Creds.Forget(runID)
		if err := s.writeAgentRunRecord(ctx, r); err != nil {
			s.Log.Warn("agentrun_record write failed", "run_id", runID, "err", err)
		}
		now := time.Now()
		var err error
		r, err = s.Store.Update(runID, func(rr *Record) error {
			rr.Local.TerminalDone, rr.Local.Revoked, rr.Local.RecordWritten = true, true, true
			setCond(&rr.AgentRun, v1.CondCredentialsIssued, metav1.ConditionFalse, "Revoked", "", now)
			return nil
		})
		if err != nil {
			return false, err
		}
		// Containers still running when the run is terminal are stopped now (R1-040).
		go s.stopContainers(context.Background(), r, []string{cAgent, cUploader, cPrepare, cGuard, cAgentd, cWatchdog, cPause}, 2)
		// Per-run log flush (M3, B-LOG-01 local stand-in): every line of every sandbox container, pushed once the pod
		// group has stopped, so short-lived containers are never missed by the collector's tailer.
		go s.flushLogs(context.Background(), r)
	}
	fin := ar.Status.FinishedAt
	if fin != nil && time.Since(fin.Time) > s.Cfg.ContainerTTL && !r.Local.ContainersGCed {
		s.gcContainers(ctx, r)
		_, _ = s.Store.Update(runID, func(rr *Record) error { rr.Local.ContainersGCed = true; return nil })
		return true, nil
	}
	// keep polling while notifications are pending or GC is due
	if len(pendingNotes(r)) == 0 {
		if fin != nil {
			wait := time.Until(fin.Time.Add(s.Cfg.ContainerTTL))
			if wait > 30*time.Second {
				return true, nil // the minute GC tick resumes this run
			}
		}
	}
	return false, nil
}

func (s *Server) writeAgentRunRecord(ctx context.Context, r *Record) error {
	ar := copyAR(&r.AgentRun)
	ar.Spec.RunGrant = ""
	for i := range ar.Status.Conditions {
		ar.Status.Conditions[i].Message = truncate(ar.Status.Conditions[i].Message, 256)
	}
	body, _ := json.MarshalIndent(ar, "", " ")
	b, k, _ := s3x.ParseURI(ar.Spec.Artifacts.ArtifactsPrefix)
	return s.S3.PutWriteOnceIdempotent(ctx, b, k+"agentrun_record/agentrun.json", body, s3x.PutOptions{
		ContentType: "application/json", Tagging: s3x.Tagging("r2y", "agentrun_record", ar.Spec.TenantID)})
}

func (s *Server) gcContainers(ctx context.Context, r *Record) {
	for _, c := range []string{cAgent, cUploader, cPrepare, cGuard, cAgentd, cWatchdog, cVolumeInit, cPause} {
		_ = s.Engine.Remove(ctx, cname(r, c))
	}
	for _, v := range volumeNames {
		_ = s.Engine.VolumeRemove(ctx, r.AgentRun.Name+"-"+v)
	}
	for v := range tmpfsVolumes {
		_ = s.Engine.VolumeRemove(ctx, r.AgentRun.Name+"-"+v)
	}
	_ = os.RemoveAll(filepath.Join(s.Cfg.StateDir, "runs", r.AgentRun.Spec.RunID))
	s.Log.Info("sandbox containers removed", "run_id", r.AgentRun.Spec.RunID)
}

// gcRecords deletes records past retention (agentrun_retention).
func (s *Server) gcRecords() {
	recs, _ := s.Store.List()
	for _, r := range recs {
		if f := r.AgentRun.Status.FinishedAt; f != nil && time.Since(f.Time) > s.Cfg.RecordRetention && r.Local.RecordWritten {
			_ = s.Store.Delete(r.AgentRun.Spec.RunID)
		}
	}
}
