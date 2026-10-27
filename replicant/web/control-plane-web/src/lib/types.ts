// Shapes from control-plane-api.openapi.yaml (subset the UI reads).
export type JobStatus =
  | "queued" | "running" | "waiting_human" | "waiting_external" | "paused"
  | "succeeded" | "failed" | "cancelled" | "rejected" | "expired" | "taken_over";

export interface JobSummary {
  job_id: string; tenant_id: string; title: string; status: JobStatus; phase: string; workflow_type: string;
  requester: string; repos: string[]; updated_at: string; created_at: string; release_bundle_id?: string;
  current_node?: string; sub_status?: string; sub_status_detail?: unknown; risk_level?: string; on_behalf_of?: string;
  open_gate?: { gate_id: string; kind: string; expires_at?: string };
  pr?: { number: number; state: string; url?: string };
  cost_usd?: number;
}

export interface RunView {
  run_id: string; node_key: string; role: string; phase: string; cycle?: number; ordinal?: number; reason?: string;
  isolation?: string; started_at?: string; finished_at?: string; result_outcome?: string; qa_verdict?: string;
  cost_usd?: number;
}

export interface GateView {
  gate_id: string; kind: string; state: string; opened_at?: string; expires_at?: string; presented_digest?: string;
  packet_uri?: string; channels?: string[]; escalation_level?: number; qa_reruns_used?: number;
  eligible?: { okta_groups?: string[]; min_repo_permission?: string; requester_may_decide?: boolean; min_approvers?: number };
  decision?: { decision: string; channel: string; comment?: string; decided_at: string; decision_id: string; principal_id: string };
  decision_log?: Array<{ decision: string; outcome: string; channel: string; decided_at: string; principal_id: string; decision_id: string }>;
}

export interface JobSnapshot {
  job_id: string; tenant_id: string; title: string; status: JobStatus; phase: string; workflow_type: string;
  requester: string; seq: number; as_of: string; cycle: number; paused: boolean; priority?: string;
  risk?: { level: string; rules_fired: string[]; raised_by_llm: boolean };
  runs?: RunView[]; gates?: GateView[];
  targets?: Array<{ repo: string; base_ref: string; base_sha?: string }>;
  terminal?: { at: string; outcome: string; reason?: string; failure_class?: string };
  pull_request?: { url?: string; repo: string; state: string; number: number; head_sha?: string };
  repair_budget?: { max: number; used: number };
  release_bundle_id?: string; memory_snapshot_id?: string;
  worker_deployment_version?: { build_id: string; deployment_name: string };
  waiting?: unknown; sub_status?: string; steering?: unknown[];
}

export interface JobDetail {
  snapshot: JobSnapshot;
  cost: { usd_so_far?: number; usd_cap?: number };
  live_run?: { run_id: string; node_key: string; phase: string; started_at?: string };
  source?: { channel?: string; url?: string };
  links: Record<string, string>;
}

export interface TimelineEvent { seq: number; type: string; at: string; summary: string; actor?: string; refs?: Record<string, string> }

export interface InboxItem {
  gate_id: string; job_id: string; tenant_id: string; kind: string; title: string; repo: string; requester: string;
  risk_level: string; opened_at: string; expires_at?: string; channels: string[]; state: string; escalation_level?: number;
  batchable?: boolean; approvals_recorded?: number; approvals_required?: number; decision?: string;
}

export interface Packet {
  packet_key: string; packet_revision: number; presented_digest: string;
  content?: {
    kind: string; revision: number; risk?: { level: string; rules_fired: string[]; raised_by_llm: boolean };
    refs?: Record<string, string>; diff_sha256?: string;
  };
  resolved?: {
    plan?: { summary?: string; steps?: Array<{ id: string; title: string; files?: string[]; rationale?: string }>;
             acceptance_criteria?: Array<{ id: string; text: string }>; risks?: string[] };
    qa_report?: { verdict: string; exit_class?: string; reason_code?: string;
                  counts?: Record<string, number>; stages?: Array<{ stage: string; status: string; duration_seconds?: number }>;
                  harness?: { name: string; version: string } };
    review_report?: { agent_report?: { verdict?: string; summary?: string; findings?: Array<Record<string, unknown>> };
                      cli?: { cli?: string; version?: string } };
    verification_report?: { checks?: string[]; passed?: boolean; findings?: unknown[]; paths?: string[] };
    handoff?: { narrative_markdown?: string; mechanical?: { diff_stats?: { files_changed: number; insertions: number; deletions: number } };
                producer?: { cli?: string; cli_version?: string; model?: string } };
  };
  links?: { diff_bundle?: string };
  resolve_error?: string;
}

export interface GateDetail {
  gate: GateView; packet: Packet; decidable: boolean; not_decidable_reason?: string; suggested_approver?: string;
}

export interface Me {
  principal_id: string; display_name: string; login?: string; groups?: string[]; platform_roles?: string[];
  tenants: Array<{ tenant_id: string; roles: string[] }>; capabilities: string[];
}

// GET /v1/me/waiting: gates the caller can decide and open PRs where they are requester or reviewer.
export interface WaitingGate {
  kind: "gate"; gate_id: string; job_id: string; tenant_id: string; gate_kind: string; title: string; repo: string;
  risk_level?: string; requester: string; opened_at: string; expires_at?: string | null; why?: string;
  waiting_since?: string; href?: string;
}

export interface WaitingPr {
  kind: "pr"; job_id: string; tenant_id: string; title: string; repo: string; requester: string;
  role: "requester" | "reviewer"; pr: { number: number; url?: string; state: string; opened_at?: string };
  waiting_since: string; idle_seconds?: number | null; last_reminded_at?: string | null; href?: string;
}

export type WaitingItem = WaitingGate | WaitingPr;

export interface WaitingResponse {
  items: WaitingItem[]; counts?: { gates: number; prs: number; total: number }; as_of?: string;
}
