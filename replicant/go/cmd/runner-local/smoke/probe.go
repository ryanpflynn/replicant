package main

import (
	"context"
	"fmt"
	"time"

	"go.temporal.io/sdk/client"
	"go.temporal.io/sdk/worker"
	"go.temporal.io/sdk/workflow"
)

// probe is the test-strategy §5.3 "Signal capture" worker: workflow ContractProbe on task queue contract-probe
// records every run.status_changed Signal and exposes them through Query `signals`.
type probe struct {
	c client.Client
	w worker.Worker
}

func ContractProbe(ctx workflow.Context) ([]map[string]any, error) {
	var got []map[string]any
	if err := workflow.SetQueryHandler(ctx, "signals", func() ([]map[string]any, error) { return got, nil }); err != nil {
		return nil, err
	}
	ch := workflow.GetSignalChannel(ctx, "run.status_changed")
	stop := workflow.GetSignalChannel(ctx, "stop")
	timer := workflow.NewTimer(ctx, 30*time.Minute)
	for {
		sel := workflow.NewSelector(ctx)
		done := false
		sel.AddReceive(ch, func(c workflow.ReceiveChannel, _ bool) {
			var m map[string]any
			c.Receive(ctx, &m)
			got = append(got, m)
		})
		sel.AddReceive(stop, func(c workflow.ReceiveChannel, _ bool) { c.Receive(ctx, nil); done = true })
		sel.AddFuture(timer, func(workflow.Future) { done = true })
		sel.Select(ctx)
		if done {
			return got, nil
		}
	}
}

func startProbe(addr, ns string) (*probe, error) {
	var c client.Client
	var err error
	for i := 0; i < 30; i++ {
		c, err = client.Dial(client.Options{HostPort: addr, Namespace: ns})
		if err == nil {
			break
		}
		time.Sleep(2 * time.Second)
	}
	if err != nil {
		return nil, err
	}
	w := worker.New(c, "contract-probe", worker.Options{})
	w.RegisterWorkflow(ContractProbe)
	if err := w.Start(); err != nil {
		return nil, err
	}
	return &probe{c: c, w: w}, nil
}

func (p *probe) begin(ctx context.Context, workflowID string) error {
	_, err := p.c.ExecuteWorkflow(ctx, client.StartWorkflowOptions{ID: workflowID, TaskQueue: "contract-probe"}, ContractProbe)
	return err
}

func (p *probe) signals(ctx context.Context, workflowID string) ([]map[string]any, error) {
	v, err := p.c.QueryWorkflow(ctx, workflowID, "", "signals")
	if err != nil {
		return nil, err
	}
	var out []map[string]any
	return out, v.Get(&out)
}

func (p *probe) stop(ctx context.Context, workflowID string) {
	_ = p.c.SignalWorkflow(ctx, workflowID, "", "stop", nil)
}

var _ = fmt.Sprint
