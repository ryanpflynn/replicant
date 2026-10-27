package main

import (
	"context"
	"encoding/json"
	"slices"
	"time"

	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"

	"example.com/replicant/internal/runner/api"
	v1 "example.com/replicant/internal/runner/v1alpha1"
)

func copyAR(ar *v1.AgentRun) *v1.AgentRun {
	b, _ := json.Marshal(ar)
	var c v1.AgentRun
	_ = json.Unmarshal(b, &c)
	return &c
}

// queueNotification records the RunStatusNotification of a phase transition (C-CRD rule 6: one per transition).
func (s *Server) queueNotification(r *Record, phase, reason string) {
	ar := &r.AgentRun
	tp, _ := s.traceparent(r)
	n := api.RunStatusNotification{
		Schema: api.NotifSchema, RunID: ar.Spec.RunID, JobID: ar.Spec.JobID, TenantID: ar.Spec.TenantID,
		IdempotencyKey: ar.Spec.IdempotencyKey, Phase: phase, Terminal: v1.RunPhase(phase).IsTerminal(),
		Reason: sanitizeReasonOpt(reason), ObservedGeneration: ar.Status.ObservedGeneration, ObservedAt: time.Now().UTC(),
		Traceparent: tp,
	}
	if n.ObservedGeneration == 0 {
		n.ObservedGeneration = 1
	}
	n.NotificationID = n.RunID + ":" + phase + ":" + itoa64(n.ObservedGeneration)
	if n.Terminal && ar.Status.ArtifactManifestURI != "" {
		n.ArtifactManifestURI = ar.Status.ArtifactManifestURI
	}
	if n.Terminal { // WS-08 M2: one span per AgentRun in the job's trace (creation -> terminal phase)
		start := ar.ObjectMeta.CreationTimestamp.Time
		if start.IsZero() {
			start = n.ObservedAt
		}
		otel("runner-local").emitJobSpan(ar.Spec.JobID, "", "runner.agentrun", 2, start, n.ObservedAt, map[string]string{
			"replicant.run_id": ar.Spec.RunID, "replicant.tenant_id": ar.Spec.TenantID, "replicant.role": string(ar.Spec.Role),
			"replicant.agentrun": ar.ObjectMeta.Name, "replicant.run_phase": phase, "replicant.run_reason": n.Reason,
			"replicant.isolation": r.Local.Isolation,
		}, phase != "Succeeded")
	}
	_, _ = s.Store.Update(ar.Spec.RunID, func(rr *Record) error {
		for _, x := range rr.Local.Notes {
			if x.NotificationID == n.NotificationID {
				return nil
			}
		}
		if slices.Contains(rr.Local.Notified, n.NotificationID) {
			return nil
		}
		rr.Local.Notes = append(rr.Local.Notes, n)
		return nil
	})
}

func sanitizeReasonOpt(r string) string {
	if r == "" {
		return ""
	}
	return sanitizeReason(r)
}

func itoa64(i int64) string {
	b, _ := json.Marshal(i)
	return string(b)
}

func pendingNotes(r *Record) []api.RunStatusNotification { return r.Local.Notes }

// deliverNotifications sends queued notifications in phase order with backoff 1 s → 5 min (C-CRD §5.1).
func (s *Server) deliverNotifications(ctx context.Context, runID string) {
	for {
		r, err := s.Store.Get(runID)
		if err != nil || r == nil || len(r.Local.Notes) == 0 {
			return
		}
		if r.Local.NotifyNextAt != nil && time.Now().Before(*r.Local.NotifyNextAt) {
			return
		}
		n := r.Local.Notes[0]
		outcome, err := s.Notifier.Signal(ctx, r.AgentRun.Spec.Callback.Namespace, r.AgentRun.Spec.Callback.WorkflowID, n)
		now := time.Now()
		if err != nil {
			_, _ = s.Store.Update(runID, func(rr *Record) error {
				rr.Local.NotifyAttempts++
				backoff := time.Second << min(rr.Local.NotifyAttempts, 9)
				if backoff > 5*time.Minute {
					backoff = 5 * time.Minute
				}
				next := now.Add(backoff)
				rr.Local.NotifyNextAt = &next
				setCond(&rr.AgentRun, v1.CondCallbackDelivered, metav1.ConditionFalse, "DeliveryFailing", truncate(n.NotificationID+": "+err.Error(), 512), now)
				return nil
			})
			if r.Local.NotifyAttempts%10 == 0 {
				s.Log.Warn("notification delivery failing", "run_id", runID, "notification_id", n.NotificationID, "err", err)
			}
			return
		}
		_, _ = s.Store.Update(runID, func(rr *Record) error {
			if len(rr.Local.Notes) > 0 && rr.Local.Notes[0].NotificationID == n.NotificationID {
				rr.Local.Notes = rr.Local.Notes[1:]
			}
			rr.Local.Notified = append(rr.Local.Notified, n.NotificationID)
			rr.Local.NotifyAttempts, rr.Local.NotifyNextAt = 0, nil
			reason := "Delivered"
			if outcome == OutWorkflowClosed {
				reason = "WorkflowClosed"
			}
			setCond(&rr.AgentRun, v1.CondCallbackDelivered, metav1.ConditionTrue, reason, n.NotificationID, now)
			return nil
		})
		s.Log.Info("notification", "run_id", runID, "notification_id", n.NotificationID, "outcome", outcome)
	}
}
