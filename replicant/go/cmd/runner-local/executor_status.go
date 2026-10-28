package main

import (
	"context"
	"encoding/json"
	"strings"
	"time"

	corev1 "k8s.io/api/core/v1"
	"k8s.io/apimachinery/pkg/api/meta"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"

	"example.com/replicant/internal/runner/controller"
	"example.com/replicant/internal/runner/s3x"
	"example.com/replicant/internal/runner/schemas"
	v1 "example.com/replicant/internal/runner/v1alpha1"
)

// observe builds the Derive observation from container state (the Pod is synthesized from the pod group).
func (s *Server) observe(ctx context.Context, r *Record) controller.Observation {
	ar := copyAR(&r.AgentRun)
	o := controller.Observation{Now: time.Now(), Run: ar, PodCreateMarked: r.Local.PodCreateMark}
	switch r.Local.Intent {
	case "deadline":
		o.Intent = controller.IntentDeadline
	case "cancel":
		o.Intent = controller.IntentCancel
	}
	if r.Local.AcceptFailure != "" {
		o.AcceptFailure = v1.FailureReason(r.Local.AcceptFailure)
	}
	pause := s.inspect(ctx, r, cPause)
	if !r.Local.PodCreateMark {
		if r.Local.PullError != "" {
			// Image pull failing before any container exists: present it as a waiting init container.
			pod := &corev1.Pod{}
			pod.Status.InitContainerStatuses = []corev1.ContainerStatus{{Name: cPrepare,
				State: corev1.ContainerState{Waiting: &corev1.ContainerStateWaiting{Reason: "ErrImagePull", Message: r.Local.PullError}}}}
			o.Pod = pod
		}
		return o
	}
	if pause == nil && r.Local.Stage != "credentials" {
		// The pod group vanished (or was never created after the marker).
		return o
	}
	pod := &corev1.Pod{}
	if pause != nil {
		pod.Spec.NodeName = "local"
	}
	status := func(name string) *corev1.ContainerStatus {
		ins := s.inspect(ctx, r, name)
		if ins == nil {
			return nil
		}
		cs := corev1.ContainerStatus{Name: name}
		switch {
		case ins.State.Running:
			cs.State.Running = &corev1.ContainerStateRunning{StartedAt: metav1.NewTime(ins.State.StartedTime())}
		case exited(ins):
			t := &corev1.ContainerStateTerminated{ExitCode: int32(ins.State.ExitCode), FinishedAt: metav1.NewTime(ins.State.FinishedTime())}
			if ins.State.OOMKilled {
				t.Reason = "OOMKilled"
			}
			cs.State.Terminated = t
		default:
			cs.State.Waiting = &corev1.ContainerStateWaiting{Reason: "ContainerCreating"}
		}
		return &cs
	}
	for _, c := range []string{cGuard, cPrepare} {
		if cs := status(c); cs != nil {
			pod.Status.InitContainerStatuses = append(pod.Status.InitContainerStatuses, *cs)
		}
	}
	for _, c := range []string{cAgent, cUploader} {
		if cs := status(c); cs != nil {
			if c == cAgent {
				cs.Started = boolp(r.Local.AgentStarted)
			}
			pod.Status.ContainerStatuses = append(pod.Status.ContainerStatuses, *cs)
		}
	}
	o.Pod = pod
	// Run lease (runner-service.md §7.3): Running/Finalizing with a stale lease while agentd still runs.
	if (ar.Status.Phase == v1.PhaseRunning || ar.Status.Phase == v1.PhaseFinalizing) && r.Local.LeaseAt != nil &&
		time.Since(*r.Local.LeaseAt) > time.Duration(s.Cfg.LeaseSeconds)*time.Second {
		if agentd := s.inspect(ctx, r, cAgentd); agentd != nil && agentd.State.Running {
			o.NodeLost = true
		}
	}
	if up := s.inspect(ctx, r, cUploader); exited(up) {
		s.observeManifest(ctx, r, &o)
	}
	return o
}

type manifestDoc struct {
	Producer struct {
		RunID string `json:"run_id"`
	} `json:"producer"`
	Partial bool `json:"partial"`
	Result  *struct {
		Key string `json:"key"`
	} `json:"result"`
	Artifacts []struct {
		BucketClass string `json:"bucket_class"`
		Key         string `json:"key"`
		SizeBytes   int64  `json:"size_bytes"`
	} `json:"artifacts"`
}

// observeManifest verifies the committed manifest (controller.md §9 "Verification").
func (s *Server) observeManifest(ctx context.Context, r *Record, o *controller.Observation) {
	ab, ak, _ := s3x.ParseURI(r.AgentRun.Spec.Artifacts.ArtifactsPrefix)
	body, _, err := s.S3.Get(ctx, ab, ak+"manifest.json")
	if err != nil {
		return
	}
	if err := schemas.Validate(schemas.RunManifest, body); err != nil {
		s.Log.Warn("manifest invalid", "run_id", r.AgentRun.Spec.RunID, "err", err)
		return
	}
	var m manifestDoc
	if json.Unmarshal(body, &m) != nil || m.Producer.RunID != r.AgentRun.Spec.RunID {
		return
	}
	for _, a := range m.Artifacts {
		b := s3x.Bucket(s.Cfg.Org, s.Cfg.Env, a.BucketClass)
		ok, size, err := s.S3.Head(ctx, b, a.Key)
		if err != nil || !ok || size != a.SizeBytes {
			s.Log.Warn("manifest object mismatch", "key", a.Key, "ok", ok, "size", size, "want", a.SizeBytes, "err", err)
			return
		}
	}
	o.ManifestPresent = true
	o.ManifestPartial = m.Partial
	if m.Result != nil {
		if ok, _, _ := s.S3.Head(ctx, ab, m.Result.Key); ok {
			o.ResultPresent = true
		}
	}
}

func lastLine(s string) string {
	lines := strings.Split(strings.TrimSpace(s), "\n")
	return strings.TrimSpace(lines[len(lines)-1])
}

// deriveAndApply runs controller.Derive and applies the decision with one record update.
func (s *Server) deriveAndApply(ctx context.Context, r *Record) (bool, error) {
	o := s.observe(ctx, r)
	d := controller.Derive(o)
	runID := r.AgentRun.Spec.RunID
	// Agent termination message → failure message (C-CRD rule 7).
	termMsg := ""
	if d.Phase.IsTerminal() && d.Phase != v1.PhaseSucceeded {
		if b, err := s.Engine.ReadFile(ctx, cname(r, cAgent), termPath, 1024); err == nil {
			var tm struct {
				Message string `json:"message"`
			}
			if json.Unmarshal(b, &tm) == nil {
				termMsg = tm.Message
			}
		}
	}
	now := time.Now()
	prev := r.AgentRun.Status.Phase
	rec, err := s.Store.Update(runID, func(rr *Record) error {
		ar := &rr.AgentRun
		// agent exit bookkeeping
		if o.Pod != nil {
			for _, cs := range o.Pod.Status.ContainerStatuses {
				if cs.Name == cAgent && cs.State.Terminated != nil && ar.Status.AgentExitedAt == nil && ar.Status.AgentStartedAt != nil {
					ar.Status.AgentExitedAt = mt(now)
					setCond(ar, v1.CondAgentRunning, metav1.ConditionFalse, "Exited", "", now)
				}
			}
		}
		if rr.Local.PrepareExit != nil && *rr.Local.PrepareExit != 0 {
			setCond(ar, v1.CondWorkspaceReady, metav1.ConditionFalse, string(prepReason(*rr.Local.PrepareExit)), rr.Local.FailureMessage, now)
		}
		if d.Phase == prev {
			return nil
		}
		ar.Status.Phase = d.Phase
		ar.Status.ObservedGeneration = ar.Generation
		if d.Phase.IsTerminal() {
			ar.Status.FinishedAt = mt(now)
			if o.ManifestPresent {
				ar.Status.ArtifactManifestURI = ar.Spec.Artifacts.ArtifactsPrefix + "manifest.json"
				if o.ResultPresent {
					ar.Status.ResultURI = ar.Spec.Artifacts.ArtifactsPrefix + "run_result/result.json"
				}
				if o.ManifestPartial {
					setCond(ar, v1.CondArtifactsUploaded, metav1.ConditionTrue, "PartialUpload", "", now)
				} else {
					setCond(ar, v1.CondArtifactsUploaded, metav1.ConditionTrue, "Uploaded", "", now)
				}
			} else if up := findCS(o.Pod, cUploader); up != nil && up.State.Terminated != nil {
				setCond(ar, v1.CondArtifactsUploaded, metav1.ConditionFalse, "UploadFailed", "", now)
			} else {
				setCond(ar, v1.CondArtifactsUploaded, metav1.ConditionUnknown, "NotStarted", "", now)
			}
			if d.Phase == v1.PhaseSucceeded {
				setCond(ar, v1.CondSucceeded, metav1.ConditionTrue, "Completed", "", now)
			} else {
				msg := d.Message
				if termMsg != "" {
					msg = termMsg
				} else if rr.Local.FailureMessage != "" && msg == "" {
					msg = rr.Local.FailureMessage
				}
				reason := d.Reason
				if reason == "" {
					reason = v1.ReasonControllerError
				}
				setCond(ar, v1.CondSucceeded, metav1.ConditionFalse, string(reason), truncate(msg, 1024), now)
			}
		}
		return nil
	})
	if err != nil {
		return false, err
	}
	if d.Phase != prev {
		reason := ""
		if c := meta.FindStatusCondition(rec.AgentRun.Status.Conditions, v1.CondSucceeded); c != nil && d.Phase.IsTerminal() {
			reason = c.Reason
		}
		s.Log.Info("phase", "run_id", runID, "from", prev, "to", d.Phase, "reason", reason)
		s.queueNotification(rec, string(d.Phase), reason)
	}
	if d.Phase.IsTerminal() && (d.DeletePodGracefully || o.NodeLost) {
		go s.stopContainers(context.Background(), rec, []string{cAgent, cUploader, cPrepare, cAgentd, cWatchdog, cPause}, 0)
	} else if d.DeletePodGracefully {
		if rec.Local.Intent == "" && !rec.AgentRun.Spec.CancelRequested {
			// startup timeout or image pull: no finalize
			go s.stopContainers(context.Background(), rec, []string{cAgent, cUploader, cPrepare, cAgentd, cWatchdog, cPause}, 0)
		} else {
			_ = s.terminateIfNeeded(ctx, rec)
		}
	}
	s.deliverNotifications(ctx, runID)
	return false, nil
}

func findCS(p *corev1.Pod, name string) *corev1.ContainerStatus {
	if p == nil {
		return nil
	}
	for i := range p.Status.ContainerStatuses {
		if p.Status.ContainerStatuses[i].Name == name {
			return &p.Status.ContainerStatuses[i]
		}
	}
	return nil
}

func prepReason(code int) v1.FailureReason {
	switch code {
	case 40:
		return v1.ReasonInputsFetchFailed
	case 41:
		return v1.ReasonDiffApplyFailed
	case 42:
		return v1.ReasonSessionRestoreFailed
	case 43:
		return v1.ReasonRepoFetchFailed
	}
	return v1.ReasonControllerError
}
