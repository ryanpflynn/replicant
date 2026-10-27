package main

import (
	"context"
	"crypto/aes"
	"crypto/cipher"
	"crypto/rand"
	"encoding/json"
	"errors"
	"fmt"
	"log/slog"
	"sync"
	"time"

	commonpb "go.temporal.io/api/common/v1"
	"go.temporal.io/api/serviceerror"
	"go.temporal.io/api/workflowservice/v1"
	"go.temporal.io/sdk/client"
	"google.golang.org/protobuf/proto"

	"example.com/replicant/internal/runner/api"
	"example.com/replicant/internal/runner/ids"
)

// Notifier is the in-process run-notifier (local-runner.md §1.2 "Notifications", parity P-28): it sends
// Temporal Signal run.status_changed to callback.workflow_id (empty run ID) with the RunStatusNotification
// as the single payload, encoded with the tenant codec (encode only, C-ARCH fourth exec role).
type Notifier struct {
	Address   string
	Namespace string
	CodecKey  []byte
	Log       *slog.Logger

	mu sync.Mutex
	c  client.Client
}

// Outcome of a delivery.
const (
	OutDelivered      = "delivered"
	OutWorkflowClosed = "workflow_closed"
)

func (n *Notifier) client() (client.Client, error) {
	n.mu.Lock()
	defer n.mu.Unlock()
	if n.c != nil {
		return n.c, nil
	}
	if n.Address == "" {
		return nil, errors.New("notifier: no Temporal address configured")
	}
	c, err := client.NewLazyClient(client.Options{HostPort: n.Address, Namespace: n.Namespace, Identity: "runner-local-notifier"})
	if err != nil {
		return nil, err
	}
	n.c = c
	return c, nil
}

// Ready checks GetSystemInfo.
func (n *Notifier) Ready(ctx context.Context) error {
	c, err := n.client()
	if err != nil {
		return err
	}
	_, err = c.WorkflowService().GetSystemInfo(ctx, &workflowservice.GetSystemInfoRequest{})
	return err
}

// Encode builds the signal payload (exported for tests).
func (n *Notifier) Encode(note api.RunStatusNotification) (*commonpb.Payload, error) {
	data, err := json.Marshal(note)
	if err != nil {
		return nil, err
	}
	plain := &commonpb.Payload{Metadata: map[string][]byte{"encoding": []byte("json/plain")}, Data: data}
	if len(n.CodecKey) == 0 {
		return plain, nil
	}
	raw, err := proto.Marshal(plain)
	if err != nil {
		return nil, err
	}
	blk, err := aes.NewCipher(n.CodecKey)
	if err != nil {
		return nil, err
	}
	gcm, err := cipher.NewGCM(blk)
	if err != nil {
		return nil, err
	}
	nonce := make([]byte, 12)
	if _, err := rand.Read(nonce); err != nil {
		return nil, err
	}
	ct := gcm.Seal(nil, nonce, raw, []byte(note.TenantID))
	return &commonpb.Payload{Metadata: map[string][]byte{
		"encoding":              []byte("binary/replicant-encrypted-v1"),
		"replicant-tenant":      []byte(note.TenantID),
		"replicant-wrapped-key": []byte("local-v1"),
	}, Data: append(nonce, ct...)}, nil
}

// Signal delivers one notification.
func (n *Notifier) Signal(ctx context.Context, namespace, workflowID string, note api.RunStatusNotification) (string, error) {
	c, err := n.client()
	if err != nil {
		return "", err
	}
	p, err := n.Encode(note)
	if err != nil {
		return "", err
	}
	if namespace == "" {
		namespace = n.Namespace
	}
	ctx, cancel := context.WithTimeout(ctx, 10*time.Second)
	defer cancel()
	_, err = c.WorkflowService().SignalWorkflowExecution(ctx, &workflowservice.SignalWorkflowExecutionRequest{
		Namespace:         namespace,
		WorkflowExecution: &commonpb.WorkflowExecution{WorkflowId: workflowID},
		SignalName:        "run.status_changed",
		Input:             &commonpb.Payloads{Payloads: []*commonpb.Payload{p}},
		Identity:          "runner-local-notifier",
		RequestId:         ids.UUID5(ids.NamespaceURL, "replicant:notification:"+note.NotificationID),
	})
	var nf *serviceerror.NotFound
	if errors.As(err, &nf) {
		return OutWorkflowClosed, nil
	}
	if err != nil {
		return "", fmt.Errorf("signal: %w", err)
	}
	return OutDelivered, nil
}
