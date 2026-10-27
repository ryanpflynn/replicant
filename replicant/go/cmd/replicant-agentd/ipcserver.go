package main

import (
	"context"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"strconv"
	"strings"
	"time"

	"example.com/replicant/internal/runner/api"
	"example.com/replicant/internal/runner/ipc"
	"example.com/replicant/internal/runner/s3x"
)

// ipcHandler serves unix:/run/replicant/ipc/agentd.sock (cli-invocation.md §5.3). It never returns a credential.
func (a *Agent) ipcHandler() http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("GET /readyz", func(w http.ResponseWriter, r *http.Request) { w.WriteHeader(200) })
	mux.HandleFunc("POST /v1/status/{phase}", func(w http.ResponseWriter, r *http.Request) {
		phase := r.PathValue("phase")
		a.evMu.Lock()
		switch phase {
		case "prepared":
			a.prepared = true
		case "finished":
			a.finished = true
		}
		a.evMu.Unlock()
		a.log.Info("status", "phase", phase)
		if phase == "finished" {
			go a.closeRun(context.Background())
		}
		w.WriteHeader(204)
	})
	mux.HandleFunc("GET /v1/events", func(w http.ResponseWriter, r *http.Request) {
		after, _ := strconv.Atoi(r.URL.Query().Get("after"))
		wait, _ := strconv.Atoi(r.URL.Query().Get("wait"))
		if wait <= 0 || wait > 30 {
			wait = 20
		}
		deadline := time.Now().Add(time.Duration(wait) * time.Second)
		go func() { // wake the cond at the deadline
			time.Sleep(time.Duration(wait) * time.Second)
			a.evMu.Lock()
			a.evCond.Broadcast()
			a.evMu.Unlock()
		}()
		a.evMu.Lock()
		for len(a.events) <= after && time.Now().Before(deadline) && r.Context().Err() == nil {
			a.evCond.Wait()
		}
		out := ipc.Events{Events: []ipc.Event{}}
		if len(a.events) > after {
			out.Events = append(out.Events, a.events[after:]...)
		}
		a.evMu.Unlock()
		writeJSON(w, 200, out)
	})
	mux.HandleFunc("GET /v1/steering/next", func(w http.ResponseWriter, r *http.Request) {
		a.steerMu.Lock()
		defer a.steerMu.Unlock()
		if len(a.steering) == 0 {
			w.WriteHeader(204)
			return
		}
		writeJSON(w, 200, a.steering[0])
	})
	mux.HandleFunc("POST /v1/steering/{id}/{state}", func(w http.ResponseWriter, r *http.Request) {
		id, state := r.PathValue("id"), r.PathValue("state")
		if state != "delivered" && state != "rejected" {
			w.WriteHeader(400)
			return
		}
		var body struct {
			Detail string `json:"detail"`
		}
		_ = json.NewDecoder(io.LimitReader(r.Body, 4096)).Decode(&body)
		a.steerMu.Lock()
		for i, m := range a.steering {
			if m.MessageID == id {
				a.steering = append(a.steering[:i], a.steering[i+1:]...)
				break
			}
		}
		if state == "delivered" {
			a.consumed = append(a.consumed, id)
		}
		a.steerMu.Unlock()
		if _, err := a.sandboxCall(r.Context(), http.MethodPost, "/sandbox/v1/messages/"+url.PathEscape(id)+":ack", api.AckRequest{State: state, Detail: body.Detail}, nil); err != nil {
			a.log.Warn("steering ack failed", "message_id", id, "err", err)
		}
		w.WriteHeader(204)
	})
	mux.HandleFunc("GET /v1/steering/consumed", func(w http.ResponseWriter, r *http.Request) {
		a.steerMu.Lock()
		defer a.steerMu.Unlock()
		writeJSON(w, 200, ipc.Consumed{MessageIDs: append([]string{}, a.consumed...)})
	})
	mux.HandleFunc("GET /v1/usage", func(w http.ResponseWriter, r *http.Request) {
		a.evMu.Lock()
		u := a.usage
		a.evMu.Unlock()
		writeJSON(w, 200, u)
	})
	mux.HandleFunc("GET /v1/turns", func(w http.ResponseWriter, r *http.Request) {
		a.evMu.Lock()
		t := a.turns
		a.evMu.Unlock()
		writeJSON(w, 200, map[string]int{"model_calls": t})
	})
	mux.HandleFunc("POST /v1/report/instructions", func(w http.ResponseWriter, r *http.Request) {
		b, _ := io.ReadAll(io.LimitReader(r.Body, 8192))
		a.log.Info("instructions loaded", "report", string(b))
		w.WriteHeader(204)
	})
	mux.HandleFunc("POST /v1/heartbeat:now", func(w http.ResponseWriter, r *http.Request) {
		ctx, cancel := context.WithTimeout(r.Context(), 3*time.Second)
		defer cancel()
		resp, err := a.heartbeatOnce(ctx)
		out := ipc.HeartbeatNow{}
		if err == nil && resp != nil {
			out.CancelReason = resp.CancelReason
		}
		writeJSON(w, 200, out)
	})
	return mux
}

// uploaderHandler serves the private uploader-ipc socket (agentd and the uploader only).
func (a *Agent) uploaderHandler() http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("GET /v1/aws-credentials", func(w http.ResponseWriter, r *http.Request) {
		a.evMu.Lock()
		done := a.manifestWritten
		a.evMu.Unlock()
		if done {
			w.WriteHeader(409)
			return
		}
		cr, err := a.awsCreds(r.Context())
		if err != nil {
			http.Error(w, err.Error(), 503)
			return
		}
		writeJSON(w, 200, ipc.AWSCredentialProcess{Version: 1, AccessKeyID: cr.AccessKeyId, SecretAccessKey: cr.SecretAccessKey,
			SessionToken: cr.Token, Expiration: cr.Expiration})
	})
	mux.HandleFunc("POST /v1/manifest-written", func(w http.ResponseWriter, r *http.Request) {
		a.evMu.Lock()
		a.manifestWritten = true
		a.evMu.Unlock()
		w.WriteHeader(204)
	})
	mux.HandleFunc("POST /v1/redact-known", func(w http.ResponseWriter, r *http.Request) {
		var in struct {
			Text string `json:"text"`
		}
		if err := json.NewDecoder(io.LimitReader(r.Body, 9<<20)).Decode(&in); err != nil {
			http.Error(w, "bad body", 400)
			return
		}
		text, hits := a.redactKnown(in.Text)
		writeJSON(w, 200, map[string]any{"text": text, "hits": hits})
	})
	return mux
}

// redactKnown replaces raw, base64 (std/url), percent- and JSON-escaped forms of every held secret.
func (a *Agent) redactKnown(text string) (string, int) {
	a.tokMu.RLock()
	var secrets []string
	for s := range a.secretsSeen {
		secrets = append(secrets, s)
	}
	a.tokMu.RUnlock()
	a.credsMu.Lock()
	if a.creds != nil {
		secrets = append(secrets, a.creds.SecretAccessKey)
		if a.creds.Token != "" {
			secrets = append(secrets, a.creds.Token)
		}
	}
	a.credsMu.Unlock()
	hits := 0
	for _, s := range secrets {
		if len(s) < 8 {
			continue
		}
		d := sha256.Sum256([]byte(s))
		mark := "[REDACTED:known-credential:" + hex.EncodeToString(d[:])[:12] + "]"
		js, _ := json.Marshal(s)
		forms := []string{s, base64.StdEncoding.EncodeToString([]byte(s)), base64.RawStdEncoding.EncodeToString([]byte(s)),
			base64.URLEncoding.EncodeToString([]byte(s)), base64.RawURLEncoding.EncodeToString([]byte(s)),
			url.QueryEscape(s), strings.Trim(string(js), `"`)}
		for _, f := range forms {
			if f == "" {
				continue
			}
			if n := strings.Count(text, f); n > 0 {
				hits += n
				text = strings.ReplaceAll(text, f, mark)
			}
		}
	}
	return text, hits
}

// awsCreds returns cached run-scoped S3 credentials, refreshed 5 min before expiry.
func (a *Agent) awsCreds(ctx context.Context) (api.AwsContainerCredentials, error) {
	a.credsMu.Lock()
	defer a.credsMu.Unlock()
	if a.creds != nil && time.Until(a.creds.Expiration) > 5*time.Minute {
		return *a.creds, nil
	}
	var cr api.AwsContainerCredentials
	if _, err := a.sandboxCall(ctx, http.MethodGet, "/sandbox/v1/credentials/aws", nil, &cr); err != nil {
		if a.creds != nil && time.Until(a.creds.Expiration) > 0 {
			return *a.creds, nil // keep the session already held (security cancel, §5)
		}
		return cr, err
	}
	a.creds = &cr
	return cr, nil
}

func (a *Agent) s3() (*s3x.Client, error) {
	cr, err := a.awsCreds(context.Background())
	if err != nil {
		return nil, err
	}
	endpoint, region := "", "us-east-1"
	if a.bs.Local != nil {
		endpoint = a.bs.Local.S3Endpoint
		if a.bs.Local.S3Region != "" {
			region = a.bs.Local.S3Region
		}
	}
	if endpoint == "" {
		return nil, fmt.Errorf("no S3 endpoint in bootstrap")
	}
	return s3x.New(s3x.Config{Endpoint: endpoint, Region: region, AccessKey: cr.AccessKeyId, SecretKey: cr.SecretAccessKey, SessionToken: cr.Token}), nil
}

func writeJSON(w http.ResponseWriter, code int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(code)
	_ = json.NewEncoder(w).Encode(v)
}
