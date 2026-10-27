"""C-IDS section 4.1 custom search attributes (registered by temporal-bootstrap)."""
from __future__ import annotations

from types import SimpleNamespace
from typing import Iterable

from temporalio.common import SearchAttributeKey, SearchAttributePair, TypedSearchAttributes

SA = SimpleNamespace(
    JOB=SearchAttributeKey.for_keyword("ReplicantJobId"),
    TENANT=SearchAttributeKey.for_keyword("ReplicantTenantId"),
    TYPE=SearchAttributeKey.for_keyword("ReplicantWorkflowType"),
    STATUS=SearchAttributeKey.for_keyword("ReplicantStatus"),
    PHASE=SearchAttributeKey.for_keyword("ReplicantPhase"),
    BUNDLE=SearchAttributeKey.for_keyword("ReplicantReleaseBundleId"),
    PARENT=SearchAttributeKey.for_keyword("ReplicantParentJobId"),
    RISK=SearchAttributeKey.for_keyword("ReplicantRiskLevel"),
    REPOS=SearchAttributeKey.for_keyword_list("ReplicantRepos"),
    OPEN_GATES=SearchAttributeKey.for_keyword_list("ReplicantOpenGateIds"),
    CYCLE=SearchAttributeKey.for_int("ReplicantRepairCycle"),
    SEQ=SearchAttributeKey.for_int("ReplicantProjectionSeq"),
    WAITING=SearchAttributeKey.for_datetime("ReplicantWaitingSince"),
    HUMAN=SearchAttributeKey.for_bool("ReplicantAwaitingHuman"),
)

# name -> Temporal CLI type, exactly the temporal-bootstrap list (for `start_local(--search-attribute ...)` in tests)
DEFINITIONS = {
    "ReplicantJobId": "Keyword", "ReplicantTenantId": "Keyword", "ReplicantWorkflowType": "Keyword",
    "ReplicantStatus": "Keyword", "ReplicantPhase": "Keyword", "ReplicantReleaseBundleId": "Keyword",
    "ReplicantParentJobId": "Keyword", "ReplicantRiskLevel": "Keyword", "ReplicantRepos": "KeywordList",
    "ReplicantOpenGateIds": "KeywordList", "ReplicantRepairCycle": "Int", "ReplicantProjectionSeq": "Int",
    "ReplicantWaitingSince": "Datetime", "ReplicantAwaitingHuman": "Bool",
}


def dev_server_args() -> list[str]:
    out: list[str] = []
    for k, v in DEFINITIONS.items():
        out += ["--search-attribute", f"{k}={v}"]
    return out


def job_start_attributes(*, job_id: str, tenant_id: str, release_bundle_id: str, workflow_type: str = "unrouted",
                         status: str = "queued", parent_job_id: str | None = None,
                         repos: Iterable[str] | None = None) -> TypedSearchAttributes:
    """Search attributes a ReplicantJob is started with (control-plane-api intake). The workflow maintains the rest."""
    pairs = [SearchAttributePair(SA.JOB, job_id), SearchAttributePair(SA.TENANT, tenant_id),
             SearchAttributePair(SA.STATUS, status), SearchAttributePair(SA.BUNDLE, release_bundle_id),
             SearchAttributePair(SA.TYPE, workflow_type)]
    if parent_job_id:
        pairs.append(SearchAttributePair(SA.PARENT, parent_job_id))
    if repos:
        pairs.append(SearchAttributePair(SA.REPOS, list(repos)))
    return TypedSearchAttributes(pairs)
