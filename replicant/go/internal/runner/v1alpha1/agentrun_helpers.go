// Hand-written companions to the generated agentrun_types.go: constants for the closed enums,
// the C-CRD §4 failure-reason table, well-known keys, and small helpers. The generated file is the
// schema (WS-02 R1-034); this file must never declare fields.
package v1alpha1

import (
	"strings"

	corev1 "k8s.io/api/core/v1"
	"k8s.io/apimachinery/pkg/api/resource"
)

const (
	RolePlanner     RunRole = "planner"
	RoleImplementer RunRole = "implementer"
	RoleQA          RunRole = "qa"
	RoleReviewer    RunRole = "reviewer"
	RoleRepair      RunRole = "repair"
	RoleTakeover    RunRole = "takeover"
	RoleEval        RunRole = "eval"

	WorkloadCoding  WorkloadClass = "coding"
	WorkloadQABuild WorkloadClass = "qa_build"
	WorkloadReview  WorkloadClass = "review"
	WorkloadUtility WorkloadClass = "utility"

	RuntimeGVisor  RuntimeClassName = "replicant-gvisor"
	RuntimeKataFC  RuntimeClassName = "replicant-kata-fc"
	RuntimeKataCLH RuntimeClassName = "replicant-kata-clh"

	BackingEmptyDir               WorkspaceBacking = "emptyDir"
	BackingGenericEphemeralVolume WorkspaceBacking = "genericEphemeralVolume"

	EnforceWatchdog         WorkspaceEnforcement = "watchdog"
	EnforceXFSProjectQuota  WorkspaceEnforcement = "xfsProjectQuota"
	EnforceGuestBlockDevice WorkspaceEnforcement = "guestBlockDevice"
	EnforceFixedSizeVolume  WorkspaceEnforcement = "fixedSizeVolume"

	// CancelReasonSecurity ends the run Failed/SecurityViolation instead of Cancelled (C-CRD §1 rule 4).
	CancelReasonSecurity CancelReason = "security"

	PhasePending      RunPhase = "Pending"
	PhaseProvisioning RunPhase = "Provisioning"
	PhaseRunning      RunPhase = "Running"
	PhaseFinalizing   RunPhase = "Finalizing"
	PhaseCancelling   RunPhase = "Cancelling"
	PhaseSucceeded    RunPhase = "Succeeded"
	PhaseFailed       RunPhase = "Failed"
	PhaseCancelled    RunPhase = "Cancelled"
)

// IsTerminal reports whether the phase is final (CEL also enforces finality).
func (p RunPhase) IsTerminal() bool {
	return p == PhaseSucceeded || p == PhaseFailed || p == PhaseCancelled
}

// Condition types (closed set, agentrun-crd.md §3).
const (
	CondAccepted          = "Accepted"
	CondCredentialsIssued = "CredentialsIssued"
	CondScheduled         = "Scheduled"
	CondWorkspaceReady    = "WorkspaceReady"
	CondAgentRunning      = "AgentRunning"
	CondArtifactsUploaded = "ArtifactsUploaded"
	CondSucceeded         = "Succeeded"
	CondCallbackDelivered = "CallbackDelivered"
	CondDiskPressure      = "DiskPressure"
)

// FailureReason is the closed set of C-CRD §4 failure reasons (C-CRD 3.0.0).
type FailureReason string

const (
	ReasonAdmissionDenied              FailureReason = "AdmissionDenied"
	ReasonRuntimeClassUnavailable      FailureReason = "RuntimeClassUnavailable"
	ReasonCredentialIssueFailed        FailureReason = "CredentialIssueFailed"
	ReasonDefinitionUnavailable        FailureReason = "DefinitionUnavailable"
	ReasonUnschedulable                FailureReason = "Unschedulable"
	ReasonImagePullFailed              FailureReason = "ImagePullFailed"
	ReasonSandboxStartTimeout          FailureReason = "SandboxStartTimeout"
	ReasonRepoFetchFailed              FailureReason = "RepoFetchFailed"
	ReasonInputsFetchFailed            FailureReason = "InputsFetchFailed"
	ReasonDiffApplyFailed              FailureReason = "DiffApplyFailed"
	ReasonSessionRestoreFailed         FailureReason = "SessionRestoreFailed"
	ReasonEnvironmentSetupFailed       FailureReason = "EnvironmentSetupFailed"
	ReasonWorkspaceDiskExceeded        FailureReason = "WorkspaceDiskExceeded"
	ReasonOOMKilled                    FailureReason = "OOMKilled"
	ReasonDeadlineExceeded             FailureReason = "DeadlineExceeded"
	ReasonAgentExitedNonZero           FailureReason = "AgentExitedNonZero"
	ReasonAgentResultMissing           FailureReason = "AgentResultMissing"
	ReasonAgentResultInvalid           FailureReason = "AgentResultInvalid"
	ReasonGatewayBudgetExceeded        FailureReason = "GatewayBudgetExceeded"
	ReasonGatewayUnavailable           FailureReason = "GatewayUnavailable"
	ReasonArtifactUploadFailed         FailureReason = "ArtifactUploadFailed"
	ReasonNodeLost                     FailureReason = "NodeLost"
	ReasonPodEvicted                   FailureReason = "PodEvicted"
	ReasonPreempted                    FailureReason = "Preempted"
	ReasonControllerError              FailureReason = "ControllerError"
	ReasonSecurityViolation            FailureReason = "SecurityViolation"
	ReasonCancelledByRequest           FailureReason = "CancelledByRequest"
	ReasonOrchestratorDeadlineExceeded FailureReason = "OrchestratorDeadlineExceeded"
)

// ManifestUnknownPrefix is the message prefix that makes ImagePullFailed non-retryable
// (C-CRD §4: a missing digest is not fixed by retrying).
const ManifestUnknownPrefix = "ImagePullFailed: manifest unknown"

// Retryable mirrors the retryable column of agentrun-crd.md §4. The message matters only for
// ImagePullFailed (R1-056).
func (r FailureReason) Retryable(message string) bool {
	switch r {
	case ReasonImagePullFailed:
		return !strings.HasPrefix(message, ManifestUnknownPrefix)
	case ReasonRuntimeClassUnavailable, ReasonCredentialIssueFailed, ReasonDefinitionUnavailable,
		ReasonUnschedulable, ReasonSandboxStartTimeout, ReasonRepoFetchFailed,
		ReasonEnvironmentSetupFailed, ReasonAgentExitedNonZero, ReasonAgentResultMissing,
		ReasonGatewayUnavailable, ReasonArtifactUploadFailed, ReasonNodeLost, ReasonPodEvicted,
		ReasonPreempted, ReasonControllerError, ReasonOrchestratorDeadlineExceeded:
		return true
	default:
		return false
	}
}

// Well-known keys (C-IDS §7–§8 plus CCR-02-03 and CCR-08-03 additions).
const (
	LabelTenant          = "replicant.example.com/tenant"
	LabelJobID           = "replicant.example.com/job-id"
	LabelRunID           = "replicant.example.com/run-id"
	LabelRole            = "replicant.example.com/role"
	LabelWorkloadClass   = "replicant.example.com/workload-class"
	LabelWorkflowType    = "replicant.example.com/workflow-type"
	LabelActingFor       = "replicant.example.com/acting-for"
	LabelIsolation       = "replicant.example.com/isolation"
	LabelBundle          = "replicant.example.com/bundle"
	LabelNetworkProfile  = "replicant.example.com/network-profile"
	LabelManagedBy       = "app.kubernetes.io/managed-by"
	ManagedByValue       = "agentrun-controller"
	LabelWorkspaceMode   = "replicant.example.com/workspace-enforcement"
	LabelWorkspaceBytes  = "replicant.example.com/workspace-bytes"
	AnnotationSpecDigest = "replicant.example.com/spec-digest"
	AnnotationIdemKey    = "replicant.example.com/idempotency-key"
	// AnnotationRuntimeOverride records an operational runtime override applied at admission
	// (runner-service.md §3.4, R2-045): "<override_id>:<from>-><to>".
	AnnotationRuntimeOverride = "replicant.example.com/runtime-override"

	// Namespace labels on sbx-<tenant> (XD-FIX-R1-031).
	NSLabelZone           = "replicant.example.com/zone" // value "sandbox"
	NSLabelTenantReady    = "replicant.example.com/tenant-ready"
	NSLabelExecKillSwitch = "replicant.example.com/exec-kill-switch"
	NSLabelMaxActiveRuns  = "replicant.example.com/max-active-runs"

	FinalizerCleanup   = "replicant.example.com/agentrun-cleanup"
	FinalizerPodStatus = "replicant.example.com/pod-status"
)

// ToResourceList converts the CRD's string quantities into a corev1.ResourceList. CRD patterns
// guarantee parsing succeeds for any admitted object.
func (q ResourceQuantities) ToResourceList() (corev1.ResourceList, error) {
	out := corev1.ResourceList{}
	for name, s := range map[corev1.ResourceName]string{
		corev1.ResourceCPU:              q.CPU,
		corev1.ResourceMemory:           q.Memory,
		corev1.ResourceEphemeralStorage: q.EphemeralStorage,
	} {
		v, err := resource.ParseQuantity(s)
		if err != nil {
			return nil, err
		}
		out[name] = v
	}
	return out, nil
}
