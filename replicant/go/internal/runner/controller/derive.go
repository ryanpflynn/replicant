// Package controller contains the pure (I/O-free) decision logic of agentrun-controller:
// it derives the next AgentRun phase and failure reason from observed child state.
// The reconcile loop (controller.md section 3) gathers observations, calls Derive,
// and applies the result with a single status update. Keeping this pure makes the
// state machine exhaustively table-testable and identical after controller restarts
// (agentrun-crd.md rule 5: phase is recomputed from observed child state).
package controller

import (
	"strings"
	"time"

	corev1 "k8s.io/api/core/v1"

	v1 "example.com/replicant/internal/runner/v1alpha1"
)

// Container names in the sandbox Pod (controller.md section 4; shape agreed with WS-03
// cli-invocation.md section 1.1, XD-03-01).
const (
	CWorkspaceGuard = "workspace-guard"  // WS-02 init: hard-limit check (fail closed)
	CAgentd         = "replicant-agentd" // WS-03 native sidecar: sole run-token holder
	CWatchdog       = "watchdog"         // WS-02 native sidecar
	CPrepare        = "prepare"          // WS-03 init: tools, inputs, git, diff, session
	CAgent          = "agent"            // WS-03 entrypoint + CLI
	CUploader       = "uploader"         // WS-02 regular container: outlives the agent
)

// Exit codes of the agent container (WS-03 cli-invocation.md section 4.6, normative).
const (
	ExitOK                    = 0
	ExitAgentNonZero          = 10
	ExitResultMissing         = 11
	ExitResultInvalid         = 12
	ExitGatewayBudget         = 20
	ExitGatewayUnavailable    = 21
	ExitWorkspaceDiskExceeded = 30
	ExitChildOOM              = 31
	ExitEnvironmentSetup      = 44 // EnvironmentSetupFailed (accepted CCR-03-01)
	ExitServiceNotReady       = 45 // EnvironmentSetupFailed
	ExitTerminated            = 143
)

// Exit codes of prepare (WS-03) and workspace-guard (WS-02).
const (
	PrepInputsFetchFailed    = 40
	PrepDiffApplyFailed      = 41
	PrepSessionRestoreFailed = 42
	PrepRepoFetchFailed      = 43
	GuardLimitNotEnforced    = 84
)

// Exit codes of the uploader.
const (
	UpOK      = 0  // full manifest committed
	UpPartial = 10 // partial manifest committed
	UpFailed  = 20 // could not commit a manifest
)

// Intent is what the controller itself has asked for. It is recorded durably in
// status (Finalizing/Cancelling phase plus the Succeeded condition reason
// "InProgress" message) before acting, so that it survives restarts.
type Intent int

const (
	IntentNone Intent = iota
	IntentDeadline
	IntentCancel
	IntentStartupTimeout
)

// Observation is everything Derive needs. The reconciler fills it from the API
// server (uncached reads for the Pod when deciding terminal transitions) and S3.
type Observation struct {
	Now             time.Time
	Run             *v1.AgentRun
	Pod             *corev1.Pod // nil if absent
	PodCreateMarked bool        // Scheduled condition already set by us before Pod creation
	// PodCreateRejection is the definite rejection of the last Pod CREATE: "" (none), "quota"
	// (ResourceQuota Forbidden: retry until startupSeconds), or "admission" (VAP/webhook deny).
	PodCreateRejection string
	// AcceptFailure is set when the controller-side Accepted checks failed definitively
	// (RuntimeClassUnavailable, AdmissionDenied for digest mismatch, DefinitionUnavailable after
	// the registry retry budget, CredentialIssueFailed after the KMS retry budget).
	AcceptFailure   v1.FailureReason
	NodeLost        bool // Pod's node NotReady/Unknown > grace, or Node object deleted
	Intent          Intent
	ManifestPresent bool // <artifactsPrefix>manifest.json exists and verified
	ManifestPartial bool
	ResultPresent   bool
}

// Decision is the outcome applied by the reconciler.
type Decision struct {
	Phase  v1.RunPhase
	Reason v1.FailureReason // empty unless Phase is Failed or Cancelled
	// Actions requested from the reconciler (idempotent).
	DeletePodGracefully bool
	// DeletePodNow: the AgentRun is terminal but its Pod is still running (for example on a
	// rejoined partitioned node). Delete immediately with gracePeriodSeconds=0 (R1-040).
	DeletePodNow bool
	Message      string // one-line summary, never artifact content (C-CRD rule 7)
}

func containerStatus(pod *corev1.Pod, name string, init bool) *corev1.ContainerStatus {
	list := pod.Status.ContainerStatuses
	if init {
		list = pod.Status.InitContainerStatuses
	}
	for i := range list {
		if list[i].Name == name {
			return &list[i]
		}
	}
	return nil
}

func terminated(cs *corev1.ContainerStatus) *corev1.ContainerStateTerminated {
	if cs == nil {
		return nil
	}
	return cs.State.Terminated
}

func hasCondition(pod *corev1.Pod, t corev1.PodConditionType, reason string) bool {
	for _, c := range pod.Status.Conditions {
		if c.Type == t && c.Status == corev1.ConditionTrue && (reason == "" || c.Reason == reason) {
			return true
		}
	}
	return false
}

// prepFailure maps a failed prepare exit to a C-CRD failure reason.
func prepFailure(code int32) v1.FailureReason {
	switch code {
	case PrepRepoFetchFailed:
		return v1.ReasonRepoFetchFailed
	case PrepInputsFetchFailed:
		return v1.ReasonInputsFetchFailed
	case PrepDiffApplyFailed:
		return v1.ReasonDiffApplyFailed
	case PrepSessionRestoreFailed:
		return v1.ReasonSessionRestoreFailed
	case ExitWorkspaceDiskExceeded:
		return v1.ReasonWorkspaceDiskExceeded
	default:
		return v1.ReasonControllerError
	}
}

// agentFailure maps the agent container's termination to a reason, given the
// controller's own intent (deadline or cancel are known only to the controller).
func agentFailure(t *corev1.ContainerStateTerminated, intent Intent) v1.FailureReason {
	if t.Reason == "OOMKilled" {
		return v1.ReasonOOMKilled
	}
	switch t.ExitCode {
	case ExitResultInvalid:
		return v1.ReasonAgentResultInvalid
	case ExitResultMissing:
		return v1.ReasonAgentResultMissing
	case ExitGatewayBudget:
		return v1.ReasonGatewayBudgetExceeded
	case ExitGatewayUnavailable:
		return v1.ReasonGatewayUnavailable
	case ExitWorkspaceDiskExceeded:
		return v1.ReasonWorkspaceDiskExceeded
	case ExitChildOOM, 137:
		return v1.ReasonOOMKilled
	case ExitEnvironmentSetup, ExitServiceNotReady:
		return v1.ReasonEnvironmentSetupFailed
	case ExitAgentNonZero:
		return v1.ReasonAgentExitedNonZero
	}
	switch intent {
	case IntentDeadline:
		return v1.ReasonDeadlineExceeded
	case IntentCancel:
		return v1.ReasonCancelledByRequest
	}
	return v1.ReasonAgentExitedNonZero
}

// podDestroyed reports failures that destroy the pod without finalization.
func podDestroyed(o Observation) (v1.FailureReason, bool) {
	if o.NodeLost {
		return v1.ReasonNodeLost, true
	}
	p := o.Pod
	if p == nil {
		return "", false
	}
	if hasCondition(p, corev1.DisruptionTarget, "PreemptionByScheduler") {
		return v1.ReasonPreempted, true
	}
	if p.Status.Phase == corev1.PodFailed && p.Status.Reason == "Evicted" {
		// kubelet ephemeral-storage evictions mean the hard limit did not fire first;
		// C-CRD maps disk overrun to WorkspaceDiskExceeded, never PodEvicted.
		if strings.Contains(p.Status.Message, "ephemeral local storage") ||
			strings.Contains(p.Status.Message, "EmptyDir volume") {
			return v1.ReasonWorkspaceDiskExceeded, true
		}
		return v1.ReasonPodEvicted, true
	}
	return "", false
}

// Derive computes the next phase. It never moves backwards and never leaves a
// terminal phase (also enforced by CEL). A run cancelled with reason "security" ends
// Failed/SecurityViolation (non-retryable) on every terminal path, including node loss,
// eviction, or a deadline while cancelling (XD-FIX-R1-223, CCR-09-09).
func Derive(o Observation) Decision {
	if o.Run.Status.Phase.IsTerminal() {
		return Decision{Phase: o.Run.Status.Phase, DeletePodNow: podStillRunning(o.Pod)}
	}
	d := derive(o)
	if o.Run.Spec.CancelRequested && o.Run.Spec.CancelReason == v1.CancelReasonSecurity && d.Phase.IsTerminal() {
		d.Phase, d.Reason = v1.PhaseFailed, v1.ReasonSecurityViolation
	}
	return d
}

func derive(o Observation) Decision {
	r := o.Run
	cur := r.Status.Phase
	if o.AcceptFailure != "" {
		return Decision{Phase: v1.PhaseFailed, Reason: o.AcceptFailure}
	}
	cancel := r.Spec.CancelRequested
	startDeadline := r.CreationTimestamp.Add(time.Duration(r.Spec.Timeouts.StartupSeconds) * time.Second)

	// No pod yet.
	if o.Pod == nil {
		switch {
		case o.PodCreateMarked && cur != v1.PhasePending:
			// The pod existed and is gone without us observing a terminal state:
			// node GC or manual deletion. Never recreate (at-most-once).
			if cancel {
				return cancelled(r)
			}
			return Decision{Phase: v1.PhaseFailed, Reason: v1.ReasonNodeLost, Message: "sandbox pod disappeared"}
		case o.PodCreateMarked && cur == v1.PhasePending && !cancel && o.PodCreateRejection == "admission":
			// The API server definitively refused the Pod (VAP or webhook): a policy problem.
			return Decision{Phase: v1.PhaseFailed, Reason: v1.ReasonAdmissionDenied, Message: "sandbox pod rejected by admission policy"}
		case o.PodCreateMarked && cur == v1.PhasePending && !cancel && o.PodCreateRejection == "quota":
			// ResourceQuota refusal: no Pod exists, so re-creating is safe. Wait for capacity.
			if o.Now.After(startDeadline) {
				return Decision{Phase: v1.PhaseFailed, Reason: v1.ReasonUnschedulable, Message: "pod quota exhausted until startupSeconds"}
			}
			return Decision{Phase: v1.PhasePending}
		case o.PodCreateMarked && cur == v1.PhasePending && !cancel:
			// Marker written but creation not confirmed (crash between the two
			// steps). Safety over liveness: fail retryably, never create twice.
			return Decision{Phase: v1.PhaseFailed, Reason: v1.ReasonControllerError, Message: "pod creation not confirmed after marker"}
		case cancel && cur == v1.PhasePending:
			// C-CRD §1: Pending → Cancelling → Cancelled; one notification per transition (rule 6).
			return Decision{Phase: v1.PhaseCancelling}
		case cancel:
			return cancelled(r)
		case o.Now.After(startDeadline):
			return Decision{Phase: v1.PhaseFailed, Reason: v1.ReasonUnschedulable}
		}
		return Decision{Phase: v1.PhasePending}
	}

	p := o.Pod
	if p.Status.Phase == corev1.PodFailed && p.Status.Reason == "DeadlineExceeded" {
		// Pod activeDeadlineSeconds backstop fired (controller was down past the deadline).
		if cancel {
			return cancelled(r)
		}
		return Decision{Phase: v1.PhaseFailed, Reason: v1.ReasonDeadlineExceeded, Message: "pod activeDeadlineSeconds backstop"}
	}
	if up := terminated(containerStatus(p, CUploader, false)); up != nil && up.Reason == "OOMKilled" {
		// The trusted uploader was OOM-killed together with the workload: the pod sandbox / VM ran
		// out of memory. No finalization is possible (C-CRD §1 Running → Failed).
		return Decision{Phase: v1.PhaseFailed, Reason: v1.ReasonOOMKilled, Message: "pod sandbox out of memory"}
	}
	if reason, ok := podDestroyed(o); ok {
		msg := ""
		if cancel {
			msg = "destroyed while cancelling" // cancel intent stays visible in spec.cancelReason
		}
		return Decision{Phase: v1.PhaseFailed, Reason: reason, Message: msg}
	}

	// A cancel that landed during Provisioning stops the group before the agent starts: once nothing in the pod is
	// still running or waiting, the cancellation is complete (C-CRD §1 Provisioning → Cancelling → Cancelled).
	if cancel && !hasContainer(p, CAgent) && !anyContainerActive(p) {
		return cancelled(r)
	}

	// Init container failures (Provisioning).
	if t := terminated(containerStatus(p, CWorkspaceGuard, true)); t != nil && t.ExitCode != 0 {
		if cancel {
			return cancelled(r)
		}
		return Decision{Phase: v1.PhaseFailed, Reason: v1.ReasonControllerError, Message: "workspace hard limit not enforced (guard exit " + itoa(t.ExitCode) + ")"}
	}
	if t := terminated(containerStatus(p, CPrepare, true)); t != nil && t.ExitCode != 0 {
		if cancel {
			return cancelled(r)
		}
		return Decision{Phase: v1.PhaseFailed, Reason: prepFailure(t.ExitCode), Message: "prepare exit " + itoa(t.ExitCode)}
	}
	allStatuses := append(append([]corev1.ContainerStatus{}, p.Status.InitContainerStatuses...), p.Status.ContainerStatuses...)
	for _, s := range allStatuses {
		if w := s.State.Waiting; w != nil && (w.Reason == "ErrImagePull" || w.Reason == "ImagePullBackOff") && o.Now.After(startDeadline) {
			msg := "ImagePullFailed: " + w.Message
			if strings.Contains(w.Message, "manifest unknown") || strings.Contains(w.Message, "not found") {
				msg = v1.ManifestUnknownPrefix + " (" + s.Name + ")"
			}
			return Decision{Phase: v1.PhaseFailed, Reason: v1.ReasonImagePullFailed, Message: msg, DeletePodGracefully: true}
		}
	}

	agent := containerStatus(p, CAgent, false)
	up := terminated(containerStatus(p, CUploader, false))
	agentT := terminated(agent)

	// Terminal: uploader finished (it is the last regular container to exit).
	if up != nil {
		if cancel || o.Intent == IntentCancel {
			return cancelled(r)
		}
		if up.ExitCode == UpFailed || !o.ManifestPresent {
			return Decision{Phase: v1.PhaseFailed, Reason: v1.ReasonArtifactUploadFailed}
		}
		if agentT == nil {
			return Decision{Phase: v1.PhaseFailed, Reason: v1.ReasonControllerError, Message: "uploader exited before agent"}
		}
		if agentT.ExitCode == ExitOK && agentT.Reason != "OOMKilled" {
			if !o.ResultPresent {
				return Decision{Phase: v1.PhaseFailed, Reason: v1.ReasonAgentResultMissing}
			}
			if o.ManifestPartial {
				return Decision{Phase: v1.PhaseFailed, Reason: v1.ReasonArtifactUploadFailed, Message: "partial manifest after exit 0"}
			}
			return Decision{Phase: v1.PhaseSucceeded}
		}
		return Decision{Phase: v1.PhaseFailed, Reason: agentFailure(agentT, o.Intent)}
	}

	// Finalizing: agent exited, or we initiated termination.
	if agentT != nil || o.Intent == IntentDeadline {
		if cancel && agentT == nil {
			return Decision{Phase: v1.PhaseCancelling, DeletePodGracefully: true}
		}
		return Decision{Phase: v1.PhaseFinalizing, DeletePodGracefully: o.Intent == IntentDeadline}
	}
	if cancel {
		return Decision{Phase: v1.PhaseCancelling, DeletePodGracefully: true}
	}

	// Running: the entrypoint's startupProbe (agent-started marker) passed.
	if agent != nil && agent.Started != nil && *agent.Started {
		return Decision{Phase: v1.PhaseRunning}
	}

	// Startup deadline while provisioning.
	if o.Now.After(startDeadline) {
		if p.Spec.NodeName == "" {
			return Decision{Phase: v1.PhaseFailed, Reason: v1.ReasonUnschedulable, DeletePodGracefully: true}
		}
		return Decision{Phase: v1.PhaseFailed, Reason: v1.ReasonSandboxStartTimeout, DeletePodGracefully: true}
	}
	if p.Spec.NodeName != "" {
		return Decision{Phase: v1.PhaseProvisioning}
	}
	return Decision{Phase: v1.PhasePending}
}

// cancelled is the terminal decision for an honored cancellation. A cancellation by
// security automation (cancelReason "security", CCR-09-04) ends Failed/SecurityViolation,
// which is non-retryable, instead of Cancelled.
func cancelled(r *v1.AgentRun) Decision {
	if r.Spec.CancelReason == v1.CancelReasonSecurity {
		return Decision{Phase: v1.PhaseFailed, Reason: v1.ReasonSecurityViolation}
	}
	return Decision{Phase: v1.PhaseCancelled, Reason: v1.ReasonCancelledByRequest}
}

// hasContainer reports whether the pod has a status for the named regular container.
func hasContainer(p *corev1.Pod, name string) bool {
	for _, cs := range p.Status.ContainerStatuses {
		if cs.Name == name {
			return true
		}
	}
	return false
}

// anyContainerActive reports whether any init or regular container is running or waiting to run.
func anyContainerActive(p *corev1.Pod) bool {
	for _, set := range [][]corev1.ContainerStatus{p.Status.InitContainerStatuses, p.Status.ContainerStatuses} {
		for _, cs := range set {
			if cs.State.Running != nil || cs.State.Waiting != nil {
				return true
			}
		}
	}
	return false
}

// podStillRunning reports whether any container of the Pod has not terminated.
func podStillRunning(p *corev1.Pod) bool {
	if p == nil || p.Status.Phase == corev1.PodSucceeded || p.Status.Phase == corev1.PodFailed {
		return false
	}
	return true
}

func itoa(i int32) string {
	if i == 0 {
		return "0"
	}
	neg := i < 0
	if neg {
		i = -i
	}
	var b [12]byte
	n := len(b)
	for i > 0 {
		n--
		b[n] = byte('0' + i%10)
		i /= 10
	}
	if neg {
		n--
		b[n] = '-'
	}
	return string(b[n:])
}
