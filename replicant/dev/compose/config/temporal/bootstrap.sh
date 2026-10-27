#!/bin/sh
# Creates namespace replicant-local and registers the C-IDS §4.1 custom search attributes.
# Idempotent. Runs after the server is healthy; every Temporal-using service waits for it.
set -eu
NS="${REPLICANT_TEMPORAL_NAMESPACE:-replicant-local}"
A="--address ${TEMPORAL_ADDRESS:-temporal:7233}"
i=0
until temporal operator cluster health $A >/dev/null 2>&1; do
  i=$((i+1)); [ "$i" -gt 60 ] && { echo "temporal not healthy"; exit 1; }; sleep 2
done
temporal operator namespace describe -n "$NS" $A >/dev/null 2>&1 || \
  temporal operator namespace create -n "$NS" --retention "${REPLICANT_NAMESPACE_RETENTION:-30d}" $A
# name:type pairs exactly as C-IDS §4.1 (8 Keyword, 2 KeywordList, 2 Int, 1 Datetime, 1 Bool).
for sa in ReplicantJobId:Keyword ReplicantTenantId:Keyword ReplicantWorkflowType:Keyword ReplicantStatus:Keyword \
          ReplicantPhase:Keyword ReplicantReleaseBundleId:Keyword ReplicantParentJobId:Keyword ReplicantRiskLevel:Keyword \
          ReplicantRepos:KeywordList ReplicantOpenGateIds:KeywordList ReplicantRepairCycle:Int \
          ReplicantProjectionSeq:Int ReplicantWaitingSince:Datetime ReplicantAwaitingHuman:Bool; do
  name="${sa%%:*}"; type="${sa##*:}"
  # create fails with AlreadyExists on re-runs; any other failure is caught by the verification below
  temporal operator search-attribute create -n "$NS" --name "$name" --type "$type" $A 2>/dev/null || true
done
missing=0
for name in ReplicantJobId ReplicantTenantId ReplicantWorkflowType ReplicantStatus ReplicantPhase ReplicantReleaseBundleId \
            ReplicantParentJobId ReplicantRiskLevel ReplicantRepos ReplicantOpenGateIds ReplicantRepairCycle \
            ReplicantProjectionSeq ReplicantWaitingSince ReplicantAwaitingHuman; do
  temporal operator search-attribute list -n "$NS" $A -o json | grep -q "\"$name\"" || { echo "missing $name"; missing=1; }
done
[ "$missing" -eq 0 ] || exit 1
echo "namespace $NS ready"
