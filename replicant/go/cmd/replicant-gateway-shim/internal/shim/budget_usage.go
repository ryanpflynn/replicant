package shim

import (
	"fmt"
	"net/http"
	"strconv"
	"strings"
	"time"

	"example.com/replicant/cmd/replicant-gateway-shim/internal/usage"
)

// levels builds the admission levels in gateway.md §6.2 order.
func (s *Server) levels(rc *reqCtx, c *Caller, q *TenantQuota) []Level {
	now := s.now().UTC()
	month, day := now.Format("200601"), now.Format("20060102")
	var lv []Level
	if c.Kind == "run" {
		var lim *int64
		if c.Run.BudgetUSD != nil {
			v := USDToMicros(*c.Run.BudgetUSD)
			lim = &v
		} else if s.Cfg.DevRunMicros > 0 {
			v := s.Cfg.DevRunMicros
			lim = &v
		}
		lv = append(lv, Level{Name: "run", Key: "fgw:run:" + rc.runID, Limit: lim})
	}
	switch {
	case c.Kind == "service" && rc.purpose == "embedding":
		var lim *int64
		if v := USDToMicros(s.Cfg.File.Budgets.Embeddings.PerTenantDayUSD); v > 0 {
			lim = &v
		}
		lv = append(lv, Level{Name: "tenant_embeddings_day", Key: "fgw:tenant:" + rc.tenant + ":emb:" + day, Limit: lim})
		if rc.pipeline != "" {
			var pl *int64
			if v := USDToMicros(s.Cfg.File.Budgets.Embeddings.ReembedBackfillUSDMax); v > 0 {
				pl = &v
			}
			lv = append(lv, Level{Name: "pipeline", Key: "fgw:pipeline:" + rc.pipeline, Limit: pl})
		}
	case c.Kind == "service":
		lv = append(lv, Level{Name: "job", Key: "fgw:job:" + rc.jobID + ":worker_spent"})
	default:
		jl := Level{Name: "job", Key: "fgw:job:" + rc.jobID}
		if _, ok := s.State.JobLimit(rc.jobID); !ok && s.Cfg.Backend != "enterprise" && s.Cfg.DevJobMicros > 0 {
			v := s.Cfg.DevJobMicros
			jl.Limit = &v
		}
		lv = append(lv, jl)
	}
	if rc.account != "" {
		lv = append(lv, Level{Name: "account", Key: "fgw:acct:" + rc.account})
	} else if q != nil {
		lv = append(lv, Level{Name: "tenant_period", Key: "fgw:tenant:" + rc.tenant + ":" + month, Limit: limitPtr(q.MonthlyBudgetUSD)},
			Level{Name: "tenant_period", Key: "fgw:tenant:" + rc.tenant + ":" + day, Limit: limitPtr(q.DailyBudgetUSD)})
	}
	var pd, pm *int64
	if q != nil {
		pd, pm = limitPtr(q.PlatformDailyUSDMax), limitPtr(q.PlatformMonthlyUSDMax)
	}
	lv = append(lv, Level{Name: "platform", Key: "fgw:platform:" + day, Limit: pd}, Level{Name: "platform", Key: "fgw:platform:" + month, Limit: pm})
	return lv
}

func limitPtr(usd float64) *int64 {
	if usd <= 0 {
		return nil
	}
	v := USDToMicros(usd)
	return &v
}

func (s *Server) setBudgetHeaders(h http.Header, rc *reqCtx) {
	if rc.runID != "" {
		lim, spent, _ := s.State.CounterState("fgw:run:" + rc.runID)
		if lim != nil {
			h.Set("X-Replicant-Budget-Run-Limit-Usd", usd6(*lim))
		}
		h.Set("X-Replicant-Budget-Run-Spent-Usd", usd6(spent))
	}
	if rc.jobID != "" {
		_, spent, _ := s.State.CounterState("fgw:job:" + rc.jobID)
		h.Set("X-Replicant-Budget-Job-Spent-Usd", usd6(spent))
	}
}

func microsToUSD(m int64) float64 { return float64(m) / 1e6 }

func (s *Server) counter(key string) BudgetCounter {
	lim, spent, resv := s.State.CounterState(key)
	bc := BudgetCounter{SpentUSD: microsToUSD(spent), ReservedUSD: microsToUSD(resv)}
	if lim != nil {
		v := microsToUSD(*lim)
		bc.LimitUSD = &v
	}
	return bc
}

func (s *Server) budgetState(rc *reqCtx) BudgetState {
	month := s.now().UTC().Format("200601")
	tp := s.counter("fgw:tenant:" + rc.tenant + ":" + month)
	tp.Period = s.now().UTC().Format("2006-01")
	return BudgetState{Run: s.counter("fgw:run:" + rc.runID), Job: s.counter("fgw:job:" + rc.jobID), TenantPeriod: tp}
}

func (s *Server) logReq(rc *reqCtx, status int, outcome string) {
	if s.Log == nil {
		return
	}
	s.Log.Info("gateway request", "request_id", rc.requestID, "protocol", rc.protocol, "tenant_id", rc.tenant, "job_id", rc.jobID,
		"run_id", rc.runID, "role", rc.role, "model", rc.model, "stream", rc.stream, "status", status, "outcome", outcome,
		"trace_id", rc.trace, "latency_ms", time.Since(rc.start).Milliseconds())
}

func (s *Server) emitDenied(rc *reqCtx, httpStatus int, status string) {
	s.emit(rc, Result{Status: httpStatus}, 0, "parsed", status)
}

// emit writes the usage event (GW-7) and the request log line (never bodies, GW-12).
func (s *Server) emit(rc *reqCtx, res Result, cost int64, src, status string) {
	s.logReq(rc, res.Status, status)
	// WS-08 M2: one span per model request in the job's trace, under the caller's span (traceparent) or, without
	// one, under the run's sandbox.run span (T-5b).
	if e := otel("replicant-gateway-shim"); e != nil && rc.jobID != "" {
		parent := rc.span
		if parent == "" && rc.runID != "" {
			parent = otelRunSpanID(rc.runID)
		}
		e.emitJobSpan(rc.jobID, parent, "gateway "+rc.protocol, 2, rc.start, time.Now(), map[string]string{
			"replicant.request_id": rc.requestID, "replicant.run_id": rc.runID, "replicant.tenant_id": rc.tenant,
			"replicant.role": rc.role, "gen_ai.request.model": rc.model, "replicant.gateway_status": status,
			"http.response.status_code": strconv.Itoa(res.Status), "replicant.cost_micros_usd": strconv.FormatInt(cost, 10),
			"gen_ai.usage.input_tokens":  strconv.Itoa(int(res.Usage.InputTokens)),
			"gen_ai.usage.output_tokens": strconv.Itoa(int(res.Usage.OutputTokens)),
		}, res.Status >= 500)
	}
	if s.Usage == nil || rc.tenant == "" {
		return
	}
	model := rc.model
	if model == "" {
		model = "unknown/unknown"
	}
	proto := rc.protocol
	ev := usage.Event{
		Schema: "replicant.gateway-usage/v1", EventID: rc.requestID, OccurredAt: rc.start.UTC().Format(time.RFC3339Nano),
		TenantID: rc.tenant, JobID: rc.jobID, RunID: rc.runID, Role: rc.role, ReleaseBundleID: rc.rbID,
		GatewayModelID: model, Protocol: proto, Streaming: rc.stream, Status: status, HTTPStatus: clampStatus(res.Status),
		UpstreamReqID: trunc(res.UpstreamRequestID, 128),
		Usage: usage.Tokens{InputTokens: res.Usage.InputTokens, OutputTokens: res.Usage.OutputTokens, CacheReadTokens: res.Usage.CacheReadTokens,
			CacheWriteTokens: res.Usage.CacheWriteTokens, ReasoningTokens: res.Usage.ReasoningTokens},
		Cost:      usage.Cost{MicrosUSD: cost, Source: "price_book", PriceBookVersion: 1},
		LatencyMs: time.Since(rc.start).Milliseconds(), RetryAttempt: rc.retry, TraceID: rc.trace, SpanID: rc.span,
		ActingFor: rc.actingFor, Purpose: rc.purpose, UsageSource: src, ServedModel: trunc(res.ServedModel, 200),
		PipelineID: rc.pipeline, EvaluationMode: rc.evalMode,
	}
	if rc.evalMode != "none" {
		ev.BudgetAccount = rc.account
	}
	if res.TTFB > 0 {
		t := res.TTFB.Milliseconds()
		ev.TTFBMs = &t
	}
	if rc.caller != nil {
		if rc.caller.Kind == "run" {
			ev.Caller = usage.Caller{Kind: "run", TokenJti: rc.caller.Run.Jti, GrantJti: rc.caller.Run.GrantJti}
		} else {
			ev.Caller = usage.Caller{Kind: "service", ServicePrincipal: rc.caller.Service}
			if localAllServices[rc.caller.Service] { // the event schema allows only the three data-plane principals
				switch rc.purpose {
				case "embedding":
					ev.Caller.ServicePrincipal = "svc_memory-service"
				case "eval_judge":
					ev.Caller.ServicePrincipal = "svc_eval-worker"
				default:
					ev.Caller.ServicePrincipal = "svc_replicant-worker"
				}
			}
		}
	}
	if !strings.HasPrefix(ev.Caller.TokenJti, "rtk_") {
		ev.Caller.TokenJti = ""
	}
	ba := &usage.BudgetAfter{}
	if rc.runID != "" {
		_, sp, _ := s.State.CounterState("fgw:run:" + rc.runID)
		ba.RunSpentMicros = &sp
	}
	if rc.jobID != "" {
		_, sp, _ := s.State.CounterState("fgw:job:" + rc.jobID)
		ba.JobSpentMicros = &sp
	}
	ev.BudgetAfter = ba
	if err := s.Usage.Append(ev); err != nil && s.Log != nil {
		s.Log.Error("usage event not queued", "request_id", rc.requestID, "err", err.Error())
	}
}

func clampStatus(s int) int {
	if s < 100 {
		return 499
	}
	if s > 599 {
		return 599
	}
	return s
}

func trunc(s string, n int) string {
	if len(s) > n {
		return s[:n]
	}
	return s
}

// String is for debugging.
func (l Level) String() string { return fmt.Sprintf("%s(%s)", l.Name, l.Key) }
