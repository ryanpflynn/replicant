package main

import (
	"bytes"
	"context"
	"encoding/json"
	"net/http"
	"os"
	"strconv"
	"strings"
	"time"

	"example.com/replicant/internal/runner/engine"
)

// flushLogs pushes every stdout/stderr line of the run's sandbox containers to RUNNER_LOG_FLUSH_URL (a Loki push
// endpoint; locally Alloy's loki.source.api "sandbox_flush", which feeds the same WS-08 sandbox pipeline as the
// container tailer) once all of them have stopped. Labels are the ones the collector derives from the container
// labels (sbx_tenant, sbx_job_id, ..., container), and timestamps are the engine's, so lines the tailer already
// shipped are identical entries that Loki drops as duplicates. No-op when the variable is unset.
func (s *Server) flushLogs(ctx context.Context, r *Record) {
	url := os.Getenv("RUNNER_LOG_FLUSH_URL")
	if url == "" || s.Engine == nil {
		return
	}
	names := []string{cGuard, cVolumeInit, cPrepare, cAgentd, cWatchdog, cAgent, cUploader}
	deadline := time.Now().Add(3 * time.Minute)
	for time.Now().Before(deadline) {
		running := false
		for _, c := range names {
			if in, err := s.Engine.InspectContainer(ctx, cname(r, c)); err == nil && in != nil && in.State.Running {
				running = true
			}
		}
		if !running {
			break
		}
		time.Sleep(2 * time.Second)
	}
	type stream struct {
		Stream map[string]string `json:"stream"`
		Values [][2]string       `json:"values"`
	}
	var streams []stream
	total := 0
	for _, c := range names {
		in, err := s.Engine.InspectContainer(ctx, cname(r, c))
		if err != nil || in == nil {
			continue
		}
		lines, err := s.Engine.LogsAll(ctx, cname(r, c), 16<<20)
		if err != nil || len(lines) == 0 {
			continue
		}
		lb := in.Config.Labels
		st := stream{Stream: map[string]string{
			"pod": cname(r, c), "container": c,
			"sbx_tenant": lb["replicant.example.com/tenant"], "sbx_job_id": lb["replicant.example.com/job-id"],
			"sbx_run_id": lb["replicant.example.com/run-id"], "sbx_role": lb["replicant.example.com/role"],
			"sbx_bundle": lb["replicant.example.com/bundle"], "sbx_workflow_type": lb["replicant.example.com/workflow-type"],
			"sbx_acting_for": lb["replicant.example.com/acting-for"], "namespace": "sbx-" + lb["replicant.example.com/tenant"],
			"flushed_by": "runner-local",
		}}
		for k, v := range st.Stream {
			if v == "" || strings.HasSuffix(v, "-") {
				delete(st.Stream, k)
			}
		}
		for _, l := range lines {
			st.Values = append(st.Values, [2]string{strconv.FormatInt(l.At.UnixNano(), 10), l.Line})
		}
		total += len(lines)
		streams = append(streams, st)
	}
	if len(streams) == 0 {
		return
	}
	body, _ := json.Marshal(map[string]any{"streams": streams})
	for attempt := 0; attempt < 5; attempt++ {
		req, _ := http.NewRequestWithContext(ctx, http.MethodPost, url, bytes.NewReader(body))
		req.Header.Set("Content-Type", "application/json")
		resp, err := http.DefaultClient.Do(req)
		if err == nil {
			resp.Body.Close()
			if resp.StatusCode < 300 {
				s.Log.Info("sandbox logs flushed", "run_id", r.AgentRun.Spec.RunID, "job_id", r.AgentRun.Spec.JobID,
					"containers", len(streams), "lines", total)
				return
			}
		}
		time.Sleep(time.Duration(attempt+1) * 3 * time.Second)
	}
	s.Log.Warn("sandbox log flush failed", "run_id", r.AgentRun.Spec.RunID)
}

var _ = engine.LogLine{}
