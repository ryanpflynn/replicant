// Package replicantauthz is the custom Temporal server ClaimMapper + Authorizer for the replicant
// (self-hosted Temporal, ADR-003). WS-01 builds it into the server image; WS-09 owns the policy.
//
// Facts this relies on:
//   - The default authorizer is a no-op that allows everything, so both plugins must be set
//     [verified: https://docs.temporal.io/self-hosted-guide/security, 2026-10-24].
//   - CallTarget carries `Request any`, a deserialized copy of the API request, so the Authorizer can
//     check the signal name and workflow ID of SignalWorkflowExecution
//     [verified: https://raw.githubusercontent.com/temporalio/temporal/main/common/authorization/authorizer.go, 2026-10-24].
//
// Identity set and API sets: C-MSG 2.0.0 §1 (CCR-01-08, CCR-08-06) and WS-01 temporal-platform.md §2
// (XD-FIX-R1-021); replicant-db-migrate per CCR-10-12; replicant-worker Count per CCR-01-12. Signal flood control: XD-FIX-R1-022. Reset: break-glass operators only
// (orchestrator ruling on CCR-07-12; service identities never hold it; XD-FIX-R2-048).
package replicantauthz

import (
	"context"
	"strings"
	"sync"
	"time"

	"go.temporal.io/api/workflowservice/v1"
	"go.temporal.io/server/common/authorization"
)

const ws = "/temporal.api.workflowservice.v1.WorkflowService/"

// identity is the first DNS label of the mTLS client certificate CN (<identity>.<cluster>.<env>),
// issued by the Temporal-clients intermediate CA; the ClaimMapper puts the CN in Claims.Subject.
type identity string

const (
	idControlPlaneAPI identity = "control-plane-api"
	idGithubIntegr    identity = "github-integration"
	idRunNotifier     identity = "run-notifier"
	idReplicantWorker   identity = "replicant-worker"
	idPublisherWorker identity = "publisher-worker"
	idProjector       identity = "replicant-projector"
	idEvalWorker      identity = "eval-worker"
	idAuditExporter   identity = "audit-exporter"
	idOpsWorker       identity = "ops-worker"
	idRetention       identity = "retention-sweeper"
	idReplayGate      identity = "replicant-replay-gate"
	idCodecServer     identity = "temporal-codec-server"
	idTemporalUI      identity = "temporal-ui"
	idDBMigrate       identity = "replicant-db-migrate" // CCR-10-12: migration pre-check only
)

// workerAPIs are the polling/response APIs every worker identity needs.
var workerAPIs = map[string]bool{
	ws + "PollWorkflowTaskQueue":           true,
	ws + "PollActivityTaskQueue":           true,
	ws + "RespondWorkflowTaskCompleted":    true,
	ws + "RespondWorkflowTaskFailed":       true,
	ws + "RespondActivityTaskCompleted":    true,
	ws + "RespondActivityTaskFailed":       true,
	ws + "RespondActivityTaskCanceled":     true,
	ws + "RecordActivityTaskHeartbeat":     true,
	ws + "RespondQueryTaskCompleted":       true,
	ws + "DescribeNamespace":               true,
	ws + "GetSystemInfo":                   true,
	ws + "GetWorkflowExecutionHistory":     true, // sticky cache misses
	ws + "ShutdownWorker":                  true,
	ws + "RecordWorkerHeartbeat":           true,
	ws + "DescribeWorkerDeploymentVersion": true,
}

var workerIdentities = map[identity]bool{
	idReplicantWorker: true, idPublisherWorker: true, idProjector: true, idEvalWorker: true,
	idOpsWorker: true, idRetention: true,
}

var readOnlyHistory = map[string]bool{
	ws + "ListWorkflowExecutions":      true,
	ws + "GetWorkflowExecutionHistory": true,
	ws + "DescribeNamespace":           true,
	ws + "GetSystemInfo":               true,
}

// Extra APIs per identity, on top of workerAPIs for worker identities.
var perIdentity = map[identity]map[string]bool{
	idControlPlaneAPI: {
		ws + "StartWorkflowExecution":            true,
		ws + "UpdateWorkflowExecution":           true, // gate.decide, job.*, ops.retry_node (CCR-07-12)
		ws + "PollWorkflowExecutionUpdate":       true,
		ws + "SignalWorkflowExecution":           true,
		ws + "QueryWorkflow":                     true,
		ws + "DescribeWorkflowExecution":         true,
		ws + "ListWorkflowExecutions":            true,
		ws + "CountWorkflowExecutions":           true,
		ws + "StartBatchOperation":               true, // release.notice, ops.kill_switch fan-out
		ws + "DescribeBatchOperation":            true,
		ws + "UpdateWorkflowExecutionOptions":    true,
		ws + "CreateSchedule":                    true,
		ws + "UpdateSchedule":                    true,
		ws + "DeleteSchedule":                    true,
		ws + "DescribeSchedule":                  true,
		ws + "ListSchedules":                     true,
		ws + "DescribeWorkerDeployment":          true,
		ws + "ListWorkerDeployments":             true,
		ws + "SetWorkerDeploymentCurrentVersion": true,
		ws + "SetWorkerDeploymentRampingVersion": true,
		ws + "DescribeWorkerDeploymentVersion":   true,
		ws + "DeleteWorkerDeploymentVersion":     true,
		ws + "DescribeNamespace":                 true,
		ws + "GetSystemInfo":                     true,
	},
	idGithubIntegr: {
		ws + "SignalWorkflowExecution": true, // pr.event only (checked below)
		ws + "DescribeNamespace":       true,
		ws + "GetSystemInfo":           true,
	},
	idRunNotifier: {
		ws + "SignalWorkflowExecution": true, // run.status_changed only (checked below)
		ws + "GetSystemInfo":           true,
	},
	idProjector: {
		ws + "ListWorkflowExecutions":    true,
		ws + "QueryWorkflow":             true,
		ws + "DescribeWorkflowExecution": true,
	},
	idRetention:     {ws + "ListWorkflowExecutions": true},
	idAuditExporter: readOnlyHistory,
	idReplayGate:    readOnlyHistory,
	idCodecServer:   {}, // the codec server never calls Temporal
	idDBMigrate:     {ws + "CountWorkflowExecutions": true},
	idReplicantWorker: {ws + "CountWorkflowExecutions": true}, // CCR-01-12
	idTemporalUI: {
		ws + "DescribeWorkflowExecution":   true,
		ws + "ListWorkflowExecutions":      true,
		ws + "CountWorkflowExecutions":     true,
		ws + "GetWorkflowExecutionHistory": true,
		ws + "DescribeNamespace":           true,
		ws + "ListNamespaces":              true,
		ws + "GetSystemInfo":               true,
		ws + "DescribeTaskQueue":           true,
		ws + "DescribeSchedule":            true,
		ws + "ListSchedules":               true,
	},
}

// Temporal UI writes: terminate and cancel for replicant-operators; reset only under an active
// break-glass grant (orchestrator ruling on CCR-07-12; normal repair uses Update ops.retry_node).
var operatorWrites = map[string]bool{
	ws + "TerminateWorkflowExecution":     true,
	ws + "RequestCancelWorkflowExecution": true,
}

const resetAPI = ws + "ResetWorkflowExecution"

// operatorClaim is attached by the ClaimMapper when the Temporal UI forwards an Okta-issued JWT whose
// groups include replicant-operators. BreakGlass is true only while the principal holds an approved,
// time-boxed break-glass grant (group replicant-temporal-breakglass, assigned by the access broker).
type operatorClaim struct {
	PrincipalID string
	BreakGlass  bool
}

// bucket is a token bucket for SignalWorkflowExecution per identity (XD-FIX-R1-022).
type bucket struct {
	mu     sync.Mutex
	tokens float64
	last   time.Time
	rate   float64
	burst  float64
}

func (b *bucket) take(now time.Time) bool {
	b.mu.Lock()
	defer b.mu.Unlock()
	if b.last.IsZero() {
		b.tokens, b.last = b.burst, now
	}
	b.tokens += now.Sub(b.last).Seconds() * b.rate
	if b.tokens > b.burst {
		b.tokens = b.burst
	}
	b.last = now
	if b.tokens < 1 {
		return false
	}
	b.tokens--
	return true
}

// Authorizer implements authorization.Authorizer.
type Authorizer struct {
	Namespace string // replicant-<env>

	mu      sync.Mutex
	buckets map[identity]*bucket
}

var _ authorization.Authorizer = (*Authorizer)(nil)

func allow() authorization.Result { return authorization.Result{Decision: authorization.DecisionAllow} }
func deny() authorization.Result  { return authorization.Result{Decision: authorization.DecisionDeny} }

func (a *Authorizer) signalAllowed(id identity) bool {
	a.mu.Lock()
	if a.buckets == nil {
		a.buckets = map[identity]*bucket{}
	}
	b := a.buckets[id]
	if b == nil {
		b = &bucket{rate: 200, burst: 400}
		if id == idRunNotifier {
			b = &bucket{rate: 50, burst: 200}
		}
		a.buckets[id] = b
	}
	a.mu.Unlock()
	return b.take(time.Now())
}

func signalTarget(t *authorization.CallTarget) (name, workflowID, runID string, ok bool) {
	r, ok := t.Request.(*workflowservice.SignalWorkflowExecutionRequest)
	if !ok {
		return "", "", "", false
	}
	return r.GetSignalName(), r.GetWorkflowExecution().GetWorkflowId(), r.GetWorkflowExecution().GetRunId(), true
}

func (a *Authorizer) Authorize(_ context.Context, c *authorization.Claims, t *authorization.CallTarget) (authorization.Result, error) {
	if c == nil || c.Subject == "" {
		return deny(), nil
	}
	// Cluster-internal traffic (history/matching to frontend) maps to system claims only for the
	// server's own certificate.
	if c.System&authorization.RoleAdmin != 0 {
		return allow(), nil
	}
	if t.Namespace != "" && t.Namespace != a.Namespace {
		return deny(), nil
	}
	id := identity(strings.SplitN(c.Subject, ".", 2)[0])

	if t.APIName == ws+"SignalWorkflowExecution" {
		if !perIdentity[id][t.APIName] {
			return deny(), nil
		}
		name, wfID, runID, ok := signalTarget(t)
		if !ok || !strings.HasPrefix(wfID, "job_") {
			return deny(), nil
		}
		switch id {
		case idRunNotifier:
			if name != "run.status_changed" || runID != "" {
				return deny(), nil
			}
		case idGithubIntegr:
			if name != "pr.event" {
				return deny(), nil
			}
		}
		if !a.signalAllowed(id) {
			// Rate limited: the caller sees PermissionDenied; metric replicant_temporal_authz_rate_limited_total.
			return deny(), nil
		}
		return allow(), nil
	}

	if t.APIName == resetAPI {
		if oc, ok := c.Extensions.(operatorClaim); ok && id == idTemporalUI && oc.BreakGlass {
			return allow(), nil
		}
		return deny(), nil
	}

	if id == idTemporalUI && operatorWrites[t.APIName] {
		if _, ok := c.Extensions.(operatorClaim); ok {
			return allow(), nil
		}
		return deny(), nil
	}

	if workerIdentities[id] && workerAPIs[t.APIName] {
		return allow(), nil
	}
	if perIdentity[id][t.APIName] {
		return allow(), nil
	}
	return deny(), nil
}
