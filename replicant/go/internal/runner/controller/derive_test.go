package controller

import (
	"testing"
	"time"

	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"

	v1 "example.com/replicant/internal/runner/v1alpha1"
)

func run(phase v1.RunPhase, cancel bool) *v1.AgentRun {
	return &v1.AgentRun{
		ObjectMeta: metav1.ObjectMeta{CreationTimestamp: metav1.NewTime(t0)},
		Spec: v1.AgentRunSpec{
			CancelRequested: cancel,
			Timeouts:        v1.RunTimeouts{StartupSeconds: 300, ActiveDeadlineSeconds: 3600, FinalizeSeconds: 300},
		},
		Status: v1.AgentRunStatus{Phase: phase},
	}
}

var t0 = time.Date(2026, 10, 24, 8, 0, 0, 0, time.UTC)

func secRun() *v1.AgentRun {
	r := run(v1.PhaseCancelling, true)
	r.Spec.CancelReason = v1.CancelReasonSecurity
	return r
}

func podPhase(node string, phase corev1.PodPhase, reason string, main []corev1.ContainerStatus) *corev1.Pod {
	p := pod(node, nil, main)
	p.Status.Phase = phase
	p.Status.Reason = reason
	return p
}

func waiting(name, reason, msg string) corev1.ContainerStatus {
	return corev1.ContainerStatus{Name: name, State: corev1.ContainerState{Waiting: &corev1.ContainerStateWaiting{Reason: reason, Message: msg}}}
}

func boolp(b bool) *bool { return &b }

func pod(node string, init []corev1.ContainerStatus, main []corev1.ContainerStatus) *corev1.Pod {
	return &corev1.Pod{
		Spec:   corev1.PodSpec{NodeName: node},
		Status: corev1.PodStatus{InitContainerStatuses: init, ContainerStatuses: main},
	}
}

func term(name string, code int32, reason string) corev1.ContainerStatus {
	return corev1.ContainerStatus{Name: name, State: corev1.ContainerState{Terminated: &corev1.ContainerStateTerminated{ExitCode: code, Reason: reason}}}
}

func running(name string, started bool) corev1.ContainerStatus {
	return corev1.ContainerStatus{Name: name, Started: boolp(started), State: corev1.ContainerState{Running: &corev1.ContainerStateRunning{}}}
}

func TestDerive(t *testing.T) {
	prepOK := term(CPrepare, 0, "Completed")
	cases := []struct {
		name   string
		o      Observation
		phase  v1.RunPhase
		reason v1.FailureReason
	}{
		{"pending no pod", Observation{Now: t0.Add(time.Second), Run: run(v1.PhasePending, false)}, v1.PhasePending, ""},
		{"cancel before pod (step 1)", Observation{Now: t0, Run: run(v1.PhasePending, true)}, v1.PhaseCancelling, ""},
		{"cancel before pod (step 2)", Observation{Now: t0, Run: run(v1.PhaseCancelling, true)}, v1.PhaseCancelled, v1.ReasonCancelledByRequest},
		{"quota rejection waits", Observation{Now: t0.Add(10 * time.Second), Run: run(v1.PhasePending, false), PodCreateMarked: true, PodCreateRejection: "quota"}, v1.PhasePending, ""},
		{"quota rejection past startup", Observation{Now: t0.Add(301 * time.Second), Run: run(v1.PhasePending, false), PodCreateMarked: true, PodCreateRejection: "quota"}, v1.PhaseFailed, v1.ReasonUnschedulable},
		{"admission rejection", Observation{Now: t0, Run: run(v1.PhasePending, false), PodCreateMarked: true, PodCreateRejection: "admission"}, v1.PhaseFailed, v1.ReasonAdmissionDenied},
		{"definition unavailable", Observation{Now: t0, Run: run(v1.PhasePending, false), AcceptFailure: v1.ReasonDefinitionUnavailable}, v1.PhaseFailed, v1.ReasonDefinitionUnavailable},
		{"vm oom", Observation{Now: t0, Run: run(v1.PhaseRunning, false), Pod: podPhase("n1", corev1.PodFailed, "", []corev1.ContainerStatus{term(CAgent, 137, "OOMKilled"), term(CUploader, 137, "OOMKilled")})}, v1.PhaseFailed, v1.ReasonOOMKilled},
		{"pod deadline backstop", Observation{Now: t0, Run: run(v1.PhaseRunning, false), Pod: podPhase("n1", corev1.PodFailed, "DeadlineExceeded", nil)}, v1.PhaseFailed, v1.ReasonDeadlineExceeded},
		{"main image pull", Observation{Now: t0.Add(301 * time.Second), Run: run(v1.PhaseProvisioning, false), Pod: pod("n1", []corev1.ContainerStatus{prepOK}, []corev1.ContainerStatus{waiting(CAgent, "ImagePullBackOff", "manifest unknown")})}, v1.PhaseFailed, v1.ReasonImagePullFailed},
		{"unschedulable no pod", Observation{Now: t0.Add(301 * time.Second), Run: run(v1.PhasePending, false)}, v1.PhaseFailed, v1.ReasonUnschedulable},
		{"marker without pod", Observation{Now: t0, Run: run(v1.PhasePending, false), PodCreateMarked: true}, v1.PhaseFailed, v1.ReasonControllerError},
		{"pod vanished mid-run", Observation{Now: t0, Run: run(v1.PhaseRunning, false), PodCreateMarked: true}, v1.PhaseFailed, v1.ReasonNodeLost},
		{"bound", Observation{Now: t0, Run: run(v1.PhasePending, false), Pod: pod("n1", nil, nil)}, v1.PhaseProvisioning, ""},
		{"repo fetch failed", Observation{Now: t0, Run: run(v1.PhaseProvisioning, false), Pod: pod("n1", []corev1.ContainerStatus{term(CPrepare, PrepRepoFetchFailed, "Error")}, nil)}, v1.PhaseFailed, v1.ReasonRepoFetchFailed},
		{"diff apply failed", Observation{Now: t0, Run: run(v1.PhaseProvisioning, false), Pod: pod("n1", []corev1.ContainerStatus{term(CPrepare, PrepDiffApplyFailed, "Error")}, nil)}, v1.PhaseFailed, v1.ReasonDiffApplyFailed},
		{"limit not enforced", Observation{Now: t0, Run: run(v1.PhaseProvisioning, false), Pod: pod("n1", []corev1.ContainerStatus{term(CWorkspaceGuard, GuardLimitNotEnforced, "Error")}, nil)}, v1.PhaseFailed, v1.ReasonControllerError},
		{"startup timeout", Observation{Now: t0.Add(301 * time.Second), Run: run(v1.PhaseProvisioning, false), Pod: pod("n1", nil, nil)}, v1.PhaseFailed, v1.ReasonSandboxStartTimeout},
		{"running", Observation{Now: t0, Run: run(v1.PhaseProvisioning, false), Pod: pod("n1", []corev1.ContainerStatus{prepOK}, []corev1.ContainerStatus{running(CAgent, true), running(CUploader, true)})}, v1.PhaseRunning, ""},
		{"agent exited -> finalizing", Observation{Now: t0, Run: run(v1.PhaseRunning, false), Pod: pod("n1", []corev1.ContainerStatus{prepOK}, []corev1.ContainerStatus{term(CAgent, 0, "Completed"), running(CUploader, true)})}, v1.PhaseFinalizing, ""},
		{"succeeded", Observation{Now: t0, Run: run(v1.PhaseFinalizing, false), ManifestPresent: true, ResultPresent: true, Pod: pod("n1", []corev1.ContainerStatus{prepOK}, []corev1.ContainerStatus{term(CAgent, 0, "Completed"), term(CUploader, UpOK, "Completed")})}, v1.PhaseSucceeded, ""},
		{"result missing", Observation{Now: t0, Run: run(v1.PhaseFinalizing, false), ManifestPresent: true, Pod: pod("n1", []corev1.ContainerStatus{prepOK}, []corev1.ContainerStatus{term(CAgent, 0, "Completed"), term(CUploader, UpOK, "Completed")})}, v1.PhaseFailed, v1.ReasonAgentResultMissing},
		{"disk exceeded", Observation{Now: t0, Run: run(v1.PhaseFinalizing, false), ManifestPresent: true, ManifestPartial: true, Pod: pod("n1", []corev1.ContainerStatus{prepOK}, []corev1.ContainerStatus{term(CAgent, ExitWorkspaceDiskExceeded, "Error"), term(CUploader, UpPartial, "Error")})}, v1.PhaseFailed, v1.ReasonWorkspaceDiskExceeded},
		{"oom container", Observation{Now: t0, Run: run(v1.PhaseFinalizing, false), ManifestPresent: true, Pod: pod("n1", []corev1.ContainerStatus{prepOK}, []corev1.ContainerStatus{term(CAgent, 137, "OOMKilled"), term(CUploader, UpPartial, "Error")})}, v1.PhaseFailed, v1.ReasonOOMKilled},
		{"deadline", Observation{Now: t0, Run: run(v1.PhaseFinalizing, false), Intent: IntentDeadline, ManifestPresent: true, Pod: pod("n1", []corev1.ContainerStatus{prepOK}, []corev1.ContainerStatus{term(CAgent, ExitTerminated, "Error"), term(CUploader, UpPartial, "Error")})}, v1.PhaseFailed, v1.ReasonDeadlineExceeded},
		{"upload failed", Observation{Now: t0, Run: run(v1.PhaseFinalizing, false), Pod: pod("n1", []corev1.ContainerStatus{prepOK}, []corev1.ContainerStatus{term(CAgent, 0, "Completed"), term(CUploader, UpFailed, "Error")})}, v1.PhaseFailed, v1.ReasonArtifactUploadFailed},
		{"cancel running", Observation{Now: t0, Run: run(v1.PhaseRunning, true), Pod: pod("n1", []corev1.ContainerStatus{prepOK}, []corev1.ContainerStatus{running(CAgent, true), running(CUploader, true)})}, v1.PhaseCancelling, ""},
		{"cancel during provisioning, agent never started", Observation{Now: t0, Run: run(v1.PhaseCancelling, true), Pod: pod("n1", []corev1.ContainerStatus{prepOK}, nil)}, v1.PhaseCancelled, v1.ReasonCancelledByRequest},
		{"cancel during provisioning, prepare still running", Observation{Now: t0, Run: run(v1.PhaseCancelling, true), Pod: pod("n1", []corev1.ContainerStatus{running(CPrepare, true)}, nil)}, v1.PhaseCancelling, ""},
		{"cancelled", Observation{Now: t0, Run: run(v1.PhaseCancelling, true), ManifestPresent: true, ManifestPartial: true, Pod: pod("n1", []corev1.ContainerStatus{prepOK}, []corev1.ContainerStatus{term(CAgent, ExitTerminated, "Error"), term(CUploader, UpPartial, "Error")})}, v1.PhaseCancelled, v1.ReasonCancelledByRequest},
		{"node lost running", Observation{Now: t0, Run: run(v1.PhaseRunning, false), NodeLost: true, Pod: pod("n1", nil, []corev1.ContainerStatus{running(CAgent, true)})}, v1.PhaseFailed, v1.ReasonNodeLost},
		{"node lost cancelling", Observation{Now: t0, Run: run(v1.PhaseCancelling, true), NodeLost: true, Pod: pod("n1", nil, []corev1.ContainerStatus{running(CAgent, true)})}, v1.PhaseFailed, v1.ReasonNodeLost},
		{"budget", Observation{Now: t0, Run: run(v1.PhaseFinalizing, false), ManifestPresent: true, ManifestPartial: true, Pod: pod("n1", []corev1.ContainerStatus{prepOK}, []corev1.ContainerStatus{term(CAgent, ExitGatewayBudget, "Error"), term(CUploader, UpPartial, "Error")})}, v1.PhaseFailed, v1.ReasonGatewayBudgetExceeded},
		{"env setup failed", Observation{Now: t0, Run: run(v1.PhaseFinalizing, false), ManifestPresent: true, Pod: pod("n1", []corev1.ContainerStatus{prepOK}, []corev1.ContainerStatus{term(CAgent, ExitEnvironmentSetup, "Error"), term(CUploader, UpOK, "Completed")})}, v1.PhaseFailed, v1.ReasonEnvironmentSetupFailed},
		{"security cancel", Observation{Now: t0, Run: secRun(), ManifestPresent: true, ManifestPartial: true, Pod: pod("n1", []corev1.ContainerStatus{prepOK}, []corev1.ContainerStatus{term(CAgent, ExitTerminated, "Error"), term(CUploader, UpPartial, "Error")})}, v1.PhaseFailed, v1.ReasonSecurityViolation},
		{"terminal stays", Observation{Now: t0, Run: run(v1.PhaseSucceeded, true)}, v1.PhaseSucceeded, ""},
		{"security node lost cancelling", Observation{Now: t0, Run: secRun(), NodeLost: true, Pod: pod("n1", nil, []corev1.ContainerStatus{running(CAgent, true)})}, v1.PhaseFailed, v1.ReasonSecurityViolation},
		{"security pod gone cancelling", Observation{Now: t0, Run: secRun(), PodCreateMarked: true}, v1.PhaseFailed, v1.ReasonSecurityViolation},
		{"security deadline backstop", Observation{Now: t0, Run: secRun(), Pod: podPhase("n1", corev1.PodFailed, "DeadlineExceeded", nil)}, v1.PhaseFailed, v1.ReasonSecurityViolation},
		{"security still cancelling", Observation{Now: t0, Run: secRun(), Pod: pod("n1", nil, []corev1.ContainerStatus{running(CAgent, true)})}, v1.PhaseCancelling, ""},
	}
	for _, c := range cases {
		d := Derive(c.o)
		if d.Phase != c.phase || d.Reason != c.reason {
			t.Errorf("%s: got %s/%s want %s/%s", c.name, d.Phase, d.Reason, c.phase, c.reason)
		}
	}

	// Terminal run with a still-running Pod (rejoined partitioned node) must be deleted now.
	live := pod("n1", nil, []corev1.ContainerStatus{running(CAgent, true)})
	live.Status.Phase = corev1.PodRunning
	if d := Derive(Observation{Now: t0, Run: run(v1.PhaseFailed, false), Pod: live}); !d.DeletePodNow {
		t.Errorf("terminal run with live pod: expected DeletePodNow")
	}
	if !v1.ReasonImagePullFailed.Retryable("ImagePullFailed: timeout") || v1.ReasonImagePullFailed.Retryable(v1.ManifestUnknownPrefix+" (agent)") {
		t.Errorf("ImagePullFailed retryability")
	}

	ev := pod("n1", nil, nil)
	ev.Status.Phase = corev1.PodFailed
	ev.Status.Reason = "Evicted"
	ev.Status.Message = "Usage of EmptyDir volume \"workspace\" exceeds the limit \"40Gi\"."
	if d := Derive(Observation{Now: t0, Run: run(v1.PhaseRunning, false), Pod: ev}); d.Reason != v1.ReasonWorkspaceDiskExceeded {
		t.Errorf("emptyDir eviction: got %s", d.Reason)
	}
	ev.Status.Message = "The node was low on resource: memory."
	if d := Derive(Observation{Now: t0, Run: run(v1.PhaseRunning, false), Pod: ev}); d.Reason != v1.ReasonPodEvicted {
		t.Errorf("node-pressure eviction: got %s", d.Reason)
	}
}
