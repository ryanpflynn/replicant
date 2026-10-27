package main

import (
	"strings"
	"time"

	"k8s.io/apimachinery/pkg/api/meta"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"

	"example.com/replicant/internal/runner/api"
	v1 "example.com/replicant/internal/runner/v1alpha1"
)

var isolationOf = map[v1.RuntimeClassName]string{
	v1.RuntimeGVisor: "gvisor", v1.RuntimeKataFC: "kata-fc", v1.RuntimeKataCLH: "kata-clh",
}

// toStatus maps a record to RunStatus with the same rules runner-api uses (runner-service.md §4).
func toStatus(r *Record) api.RunStatus {
	ar := &r.AgentRun
	st := api.RunStatus{
		RunID:          ar.Spec.RunID,
		TenantID:       ar.Spec.TenantID,
		JobID:          ar.Spec.JobID,
		IdempotencyKey: ar.Spec.IdempotencyKey,
		SpecDigest:     r.Local.SpecDigest,
		AgentRun:       api.AgentRunRef{Namespace: ar.Namespace, Name: ar.Name},
		Isolation:      isolationOf[ar.Spec.RuntimeClassName],
		Phase:          string(ar.Status.Phase),
		Terminal:       ar.Status.Phase.IsTerminal(),
		Conditions:     []api.Condition{},
		Timestamps: api.Timestamps{
			CreatedAt:        ar.CreationTimestamp.Time.UTC(),
			ScheduledAt:      tp(ar.Status.ScheduledAt),
			WorkspaceReadyAt: tp(ar.Status.WorkspaceReadyAt),
			AgentStartedAt:   tp(ar.Status.AgentStartedAt),
			AgentExitedAt:    tp(ar.Status.AgentExitedAt),
			FinishedAt:       tp(ar.Status.FinishedAt),
		},
		ArtifactManifestURI: ar.Status.ArtifactManifestURI,
		ResultURI:           ar.Status.ResultURI,
		ObservedGeneration:  ar.Status.ObservedGeneration,
	}
	if st.Phase == "" {
		st.Phase = string(v1.PhasePending)
	}
	for _, c := range ar.Status.Conditions {
		st.Conditions = append(st.Conditions, api.Condition{
			Type: c.Type, Status: string(c.Status), Reason: c.Reason, Message: truncate(c.Message, 32768),
			LastTransitionTime: c.LastTransitionTime.Time.UTC(), ObservedGeneration: c.ObservedGeneration,
		})
	}
	if ar.Status.Phase == v1.PhaseFailed || ar.Status.Phase == v1.PhaseCancelled {
		if c := meta.FindStatusCondition(ar.Status.Conditions, v1.CondSucceeded); c != nil {
			reason := v1.FailureReason(c.Reason)
			st.Failure = &api.Failure{Reason: c.Reason, Message: truncate(c.Message, 32768), Retryable: reason.Retryable(c.Message)}
		}
	}
	return st
}

func tp(t *metav1.Time) *time.Time {
	if t == nil || t.IsZero() {
		return nil
	}
	u := t.Time.UTC()
	return &u
}

func truncate(s string, n int) string {
	if len(s) <= n {
		return s
	}
	return s[:n]
}

// setCond sets a condition (lastTransitionTime changes only when status changes).
func setCond(ar *v1.AgentRun, typ string, status metav1.ConditionStatus, reason, msg string, now time.Time) bool {
	reason = sanitizeReason(reason)
	before := meta.FindStatusCondition(ar.Status.Conditions, typ)
	if before != nil && before.Status == status && before.Reason == reason && before.Message == msg {
		return false
	}
	meta.SetStatusCondition(&ar.Status.Conditions, metav1.Condition{
		Type: typ, Status: status, Reason: reason, Message: msg,
		LastTransitionTime: metav1.NewTime(now), ObservedGeneration: ar.Status.ObservedGeneration,
	})
	return true
}

func sanitizeReason(r string) string {
	if r == "" {
		return "Unknown"
	}
	var b strings.Builder
	for _, c := range r {
		if (c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') || (c >= '0' && c <= '9') || c == '_' || c == ',' || c == ':' {
			b.WriteRune(c)
		}
	}
	return b.String()
}

func mt(t time.Time) *metav1.Time { m := metav1.NewTime(t); return &m }
