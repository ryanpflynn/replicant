package main

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"math/rand"
	"sort"
	"strconv"
	"strings"
	"time"

	"example.com/replicant/internal/runner/api"
	"example.com/replicant/internal/runner/s3x"
)

// Inbox implements the steering inbox and control keys on S3 (runner-service.md §5–§6, C-ART §2).
type Inbox struct {
	S3 *s3x.Client
}

func inboxLoc(prefix string) (bucket, key string) {
	b, k, _ := s3x.ParseURI(prefix)
	return b, k + "inbox/"
}

func ctrlTags(tenant string) string { return s3x.Tagging("r90d", "steering_control", tenant) }

// WriteControl writes inbox/_control.json once (If-None-Match: *); an existing key is left as is.
func (in *Inbox) WriteControl(ctx context.Context, prefix, tenant, reason string) error {
	b, k := inboxLoc(prefix)
	body, _ := json.Marshal(map[string]any{"cancel_reason": reason, "at": time.Now().UTC().Format(time.RFC3339)})
	_, err := in.S3.Put(ctx, b, k+"_control.json", body, s3x.PutOptions{ContentType: "application/json", Tagging: ctrlTags(tenant), WriteOnce: true})
	if errors.Is(err, s3x.ErrPrecondition) {
		return nil
	}
	return err
}

// ReadControl returns the cancel_reason in inbox/_control.json, or "".
func (in *Inbox) ReadControl(ctx context.Context, prefix string) (string, error) {
	b, k := inboxLoc(prefix)
	body, _, err := in.S3.Get(ctx, b, k+"_control.json")
	if errors.Is(err, s3x.ErrNotFound) {
		return "", nil
	}
	if err != nil {
		return "", err
	}
	var c struct {
		CancelReason string `json:"cancel_reason"`
	}
	_ = json.Unmarshal(body, &c)
	return c.CancelReason, nil
}

type inboxObj struct {
	seq int
	id  string
	key string
}

func (in *Inbox) list(ctx context.Context, prefix string) ([]inboxObj, map[string]bool, error) {
	b, k := inboxLoc(prefix)
	keys, err := in.S3.List(ctx, b, k, 0)
	if err != nil {
		return nil, nil, err
	}
	var msgs []inboxObj
	acks := map[string]bool{}
	for _, key := range keys {
		name := strings.TrimPrefix(key, k)
		switch {
		case strings.HasPrefix(name, "ack-") && strings.HasSuffix(name, ".json"):
			acks[strings.TrimSuffix(strings.TrimPrefix(name, "ack-"), ".json")] = true
		case strings.HasPrefix(name, "_"):
		default:
			seqS, rest, ok := strings.Cut(strings.TrimSuffix(name, ".json"), "-")
			if !ok {
				continue
			}
			seq, err := strconv.Atoi(seqS)
			if err != nil {
				continue
			}
			msgs = append(msgs, inboxObj{seq: seq, id: rest, key: key})
		}
	}
	sort.Slice(msgs, func(i, j int) bool { return msgs[i].seq < msgs[j].seq })
	return msgs, acks, nil
}

// Post stores a message; it returns (status, created).
func (in *Inbox) Post(ctx context.Context, prefix, runID, tenant string, m api.RunMessage) (api.RunMessageStatus, bool, error) {
	msgs, acks, err := in.list(ctx, prefix)
	if err != nil {
		return api.RunMessageStatus{}, false, err
	}
	for _, o := range msgs {
		if o.id == m.MessageID {
			st := "queued"
			if acks[m.MessageID] {
				st, _ = in.ackState(ctx, prefix, m.MessageID)
			}
			return api.RunMessageStatus{MessageID: m.MessageID, RunID: runID, Seq: o.seq, State: st}, false, nil
		}
	}
	b, k := inboxLoc(prefix)
	var seq int
	for attempt := 0; attempt < 10; attempt++ {
		head, etag, err := in.S3.Get(ctx, b, k+"_head.json")
		next := 1
		opts := s3x.PutOptions{ContentType: "application/json", Tagging: ctrlTags(tenant)}
		if errors.Is(err, s3x.ErrNotFound) {
			opts.WriteOnce = true
		} else if err != nil {
			return api.RunMessageStatus{}, false, err
		} else {
			var h struct {
				NextSeq int `json:"next_seq"`
			}
			_ = json.Unmarshal(head, &h)
			if h.NextSeq > 0 {
				next = h.NextSeq
			}
			opts.IfMatch = etag
		}
		nb, _ := json.Marshal(map[string]int{"next_seq": next + 1})
		if _, err := in.S3.Put(ctx, b, k+"_head.json", nb, opts); err != nil {
			if errors.Is(err, s3x.ErrPrecondition) {
				time.Sleep(time.Duration(20+rand.Intn(80)) * time.Millisecond)
				continue
			}
			return api.RunMessageStatus{}, false, err
		}
		seq = next
		break
	}
	if seq == 0 {
		return api.RunMessageStatus{}, false, fmt.Errorf("inbox head contention")
	}
	body, _ := json.Marshal(api.InboxMessage{Seq: seq, MessageID: m.MessageID, Text: m.Text, Author: m.Author, SentAt: m.SentAt})
	key := fmt.Sprintf("%s%06d-%s.json", k, seq, m.MessageID)
	if _, err := in.S3.Put(ctx, b, key, body, s3x.PutOptions{ContentType: "application/json", WriteOnce: true,
		Tagging: s3x.Tagging("r90d", "steering_message", tenant)}); err != nil && !errors.Is(err, s3x.ErrPrecondition) {
		return api.RunMessageStatus{}, false, err
	}
	now := time.Now().UTC()
	return api.RunMessageStatus{MessageID: m.MessageID, RunID: runID, Seq: seq, State: "queued", UpdatedAt: &now}, true, nil
}

func (in *Inbox) ackState(ctx context.Context, prefix, id string) (string, error) {
	b, k := inboxLoc(prefix)
	body, _, err := in.S3.Get(ctx, b, k+"ack-"+id+".json")
	if err != nil {
		return "", err
	}
	var a struct {
		State string `json:"state"`
	}
	_ = json.Unmarshal(body, &a)
	if a.State == "" {
		a.State = "delivered"
	}
	return a.State, nil
}

// Status returns the delivery status of a message.
func (in *Inbox) Status(ctx context.Context, prefix, runID, id string, terminal bool) (*api.RunMessageStatus, error) {
	msgs, acks, err := in.list(ctx, prefix)
	if err != nil {
		return nil, err
	}
	for _, o := range msgs {
		if o.id != id {
			continue
		}
		st := "queued"
		if acks[id] {
			st, _ = in.ackState(ctx, prefix, id)
		} else if terminal {
			st = "expired"
		}
		return &api.RunMessageStatus{MessageID: id, RunID: runID, Seq: o.seq, State: st}, nil
	}
	return nil, nil
}

// Poll returns messages with seq > after, waiting up to wait.
func (in *Inbox) Poll(ctx context.Context, prefix string, after int, wait time.Duration) ([]api.InboxMessage, error) {
	deadline := time.Now().Add(wait)
	for {
		msgs, _, err := in.list(ctx, prefix)
		if err != nil {
			return nil, err
		}
		b, _ := inboxLoc(prefix)
		var out []api.InboxMessage
		seen := map[string]bool{}
		for _, o := range msgs {
			if o.seq <= after || seen[o.id] || len(out) >= 50 {
				continue
			}
			body, _, err := in.S3.Get(ctx, b, o.key)
			if err != nil {
				continue
			}
			var m api.InboxMessage
			if json.Unmarshal(body, &m) == nil {
				out = append(out, m)
				seen[o.id] = true
			}
		}
		if len(out) > 0 || time.Now().After(deadline) {
			if out == nil {
				out = []api.InboxMessage{}
			}
			return out, nil
		}
		select {
		case <-ctx.Done():
			return []api.InboxMessage{}, nil
		case <-time.After(time.Second):
		}
	}
}

// Ack writes inbox/ack-<id>.json once.
func (in *Inbox) Ack(ctx context.Context, prefix, tenant, id string, a api.AckRequest) error {
	b, k := inboxLoc(prefix)
	body, _ := json.Marshal(map[string]any{"state": a.State, "detail": a.Detail, "at": time.Now().UTC().Format(time.RFC3339)})
	_, err := in.S3.Put(ctx, b, k+"ack-"+id+".json", body, s3x.PutOptions{ContentType: "application/json", WriteOnce: true, Tagging: ctrlTags(tenant)})
	if errors.Is(err, s3x.ErrPrecondition) {
		return nil
	}
	return err
}
