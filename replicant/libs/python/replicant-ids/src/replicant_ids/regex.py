"""§12 normative validation regexes."""
from __future__ import annotations

import re

_B = "[0-9a-hjkmnp-tv-z]{26}"
_LA = r"[a-z][a-z0-9_]{0,40}\.c[0-9]{1,3}\.n[0-9]{1,3}\.[a-z][a-z0-9_]{0,30}"

PATTERNS: dict[str, str] = {
    "tenant_id": r"^[a-z][a-z0-9]*(-[a-z0-9]+)*$",
    "principal_id": rf"^(usr_{_B}|svc_[a-z][a-z0-9-]{{2,40}})$",
    "job_id": rf"^job_{_B}$",
    "request_id": rf"^jrq_{_B}$",
    "run_id": rf"^run_{_B}$",
    "agentrun_name": rf"^ar-{_B}$",
    "gate_id": rf"^gate_{_B}$",
    "decision_id": rf"^dec_{_B}$",
    "message_id": rf"^msg_{_B}$",
    "action_id": rf"^act_{_B}$",
    "notice_id": rf"^ntc_{_B}$",
    "run_grant_id": rf"^rgr_{_B}$",
    "run_token_id": rf"^rtk_{_B}$",
    "pipeline_id": rf"^rpl_{_B}$",
    "comparison_id": rf"^cmp_{_B}$",
    "proposal_id": rf"^prop_{_B}$",
    "label_id": rf"^lbl_{_B}$",
    "memory_admin_id": rf"^(mcfl|mqtn|mprg|mexp|mact)_{_B}$",
    "control_plane_id": rf"^(art|dlg|opr|lnk|grp|ntf)_{_B}$",
    "deletion_id": rf"^del_{_B}$",
    "schedule_id": rf"^sch_{_B}$",
    "leak_purge_id": rf"^leak_{_B}$",
    "eval_run_id": rf"^eval_{_B}$",
    "golden_task_id": rf"^gtask_{_B}$",
    "takeover_id": rf"^tko_{_B}$",
    "span_id": r"^(?!0{16})[0-9a-f]{16}$",
    "system_workflow_id": r"^sys:[a-z][a-z0-9-]{1,60}(:[A-Za-z0-9_.:-]{1,120})?$",
    "gateway_usage_id": rf"^gwu_{_B}$",
    "rightsizing_proposal_id": rf"^rsp_{_B}$",
    "publish_id": rf"^pub_{_B}$",
    "reconciliation_id": rf"^rec_{_B}$",
    "release_bundle_id": rf"^rb_{_B}$",
    "memory_entry_id": rf"^mem_{_B}$",
    "memory_entry_ref": rf"^mem_{_B}@[1-9][0-9]*$",
    "memory_snapshot_id": rf"^msnap_{_B}$",
    "logical_activity_id": rf"^{_LA}$",
    "idempotency_key": rf"^ik1:job_{_B}(:[a-z][a-z0-9_]{{1,30}}:[1-9][0-9]*)*:{_LA}$",
    "workflow_id_job": rf"^job_{_B}(:[a-z][a-z0-9_]{{1,30}}:[1-9][0-9]*)?$",
    "definition_ref": r"^(agent|environment|graph|policy|prompt|cli|model|nodecatalog)/[a-z0-9][a-z0-9._/-]{0,127}@[1-9][0-9]*(#sha256:[0-9a-f]{64})?$",
    "digest": r"^sha256:[0-9a-f]{64}$",
    "trace_id": r"^(?!0{32})[0-9a-f]{32}$",
    "generic": rf"^[a-z]{{2,6}}_{_B}$",
}
_COMPILED = {k: re.compile(v) for k, v in PATTERNS.items()}

# Reserved tenants and forbidden tenant prefixes (§2 tenant_id row).
RESERVED_TENANTS = frozenset({"replicant-eval", "replicant-system", "local-dev"})
_FORBIDDEN_TENANT_PREFIXES = ("sbx", "kube", "replicant-", "aws")


def is_valid(kind: str, value: str) -> bool:
    if not isinstance(value, str) or not _COMPILED[kind].match(value):
        return False
    if kind == "tenant_id":
        if not 3 <= len(value) <= 32:
            return False
        if value not in RESERVED_TENANTS and value.startswith(_FORBIDDEN_TENANT_PREFIXES):
            return False
    if kind == "logical_activity_id" and len(value) > 120:
        return False
    return True


def validate(kind: str, value: str) -> str:
    """Return value unchanged, or raise ValueError naming the kind."""
    if not is_valid(kind, value):
        raise ValueError(f"invalid {kind}: {value!r}")
    return value
