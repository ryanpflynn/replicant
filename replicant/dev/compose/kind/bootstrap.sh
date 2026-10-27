#!/usr/bin/env bash
# k8s profile bootstrap (runs inside the dev-k8s-tools container with the host's container socket).
# Tools pinned in dev/k8s-tools/Dockerfile: kind v0.33.0, kubectl v1.36.x, helm v4.3.1, cilium-cli.
set -euo pipefail
NAME="${KIND_CLUSTER_NAME:-replicant-local}"
CTX="kind-${NAME}"
REG="${REPLICANT_IMAGE_REGISTRY}"; TAG="${REPLICANT_IMAGE_TAG}"

kvm_available() {
  # The check runs in a throwaway privileged container so it sees the Linux VM/host, not this container.
  docker run --rm --privileged alpine:3.22.6 sh -c 'test -c /dev/kvm' >/dev/null 2>&1
}
case "${REPLICANT_KATA:-auto}" in
  on) KATA=on ;;
  off) KATA=off ;;
  auto) if kvm_available; then KATA=on; else KATA=off; fi ;;
esac
echo "kata: $KATA"

cfg=/tmp/kind-config.yaml
cp /kind/kind-config.yaml "$cfg"
if [ "$KATA" = on ]; then
  # Insert the /dev/kvm mount under the worker node.
  sed -i 's|^    # KVM_MOUNT.*$|    extraMounts: [{hostPath: /dev/kvm, containerPath: /dev/kvm}]|' "$cfg"
  sed -i '/^    #   extraMounts:/d' "$cfg"
fi

kind get clusters | grep -qx "$NAME" || kind create cluster --config "$cfg" --image "${KIND_NODE_IMAGE}" --wait 0s

# Cilium (CNI + CiliumNetworkPolicy), pinned.
helm repo add cilium https://helm.cilium.io >/dev/null
helm upgrade --install cilium cilium/cilium --version 1.20.4 --namespace kube-system \
  --kube-context "$CTX" --set kubeProxyReplacement=true --set k8sServiceHost="${NAME}-control-plane" \
  --set k8sServicePort=6443 --set ipam.mode=kubernetes \
  --set socketLB.hostNamespaceOnly=true --wait   # ClusterIPs from Kata/gVisor pods (WS-02 F-17, XD-02-11)

# Attach nodes to the compose networks so pods can reach Temporal, S3 and sbx-edge, and the worker can reach
# runner-api by node name.
for node in $(kind get nodes --name "$NAME"); do
  for net in ${REPLICANT_COMPOSE_NETWORKS//,/ }; do
    docker network connect "$net" "$node" 2>/dev/null || true
  done
done

# Kata (only when /dev/kvm is present): kata-deploy creates handlers kata-clh, kata-fc (plus others).
if [ "$KATA" = on ]; then
  helm upgrade --install kata-deploy oci://ghcr.io/kata-containers/kata-deploy-charts/kata-deploy \
    --version 4.3.1 --namespace kube-system --kube-context "$CTX" \
    --set "shims.disableAll=true" --set "shims.clh-runtime-rs.enabled=true" --wait
  # Firecracker stays disabled (needs a devmapper thin-pool; prod policy never selects it).
  kubectl --context "$CTX" apply -f /kind/runtimeclasses-fallback.yaml     # degraded fc and gvisor first
  kubectl --context "$CTX" apply -f /kind/runtimeclasses.yaml              # then real replicant-kata-clh
  kubectl --context "$CTX" get runtimeclass replicant-kata-clh -o jsonpath='{.handler}' | grep -qx kata-clh-runtime-rs \
    || { echo "kata-deploy did not create handler kata-clh-runtime-rs"; exit 1; }
else
  kubectl --context "$CTX" apply -f /kind/runtimeclasses-fallback.yaml
fi
if [ "${REPLICANT_GVISOR:-off}" = on ]; then
  kubectl --context "$CTX" apply -f /kind/runtimeclass-gvisor.yaml        # requires the runsc node image variant
fi
# WS-09 admission policies (vap-agentrun-binding, vap-sandbox-pods), applied unmodified except the image rule:
# locally built images are `localhost/replicant/<name>:<tag>` without a registry digest, so the ECR-and-digest
# registry host becomes `localhost/` and a tag is accepted besides a digest (parity.md P-31). Every other CEL rule is the prod one.
ECR_HOST_RE='^[0-9]{12}[.]dkr[.]ecr[.]us-east-1[.]amazonaws[.]com/'
DIGEST_RE='@sha256:[0-9a-f]{64}$'
for f in /security/kubernetes/vap-agentrun-binding.yaml /security/kubernetes/vap-sandbox-pods.yaml; do
  A="$ECR_HOST_RE" B="$DIGEST_RE" perl -pe 'if (/image\.matches/) { s/\Q$ENV{A}\E/^localhost\//g; s/\Q$ENV{B}\E/(:[A-Za-z0-9._-]{1,128}|\@sha256:[0-9a-f]{64})\$/g }' "$f" \
    | kubectl --context "$CTX" apply -f -
done

# Load replicant images (built by `replicant dev build`) into the nodes.
IMAGES="$(echo "${REPLICANT_IMAGES_TO_LOAD},${REPLICANT_IMAGES_TO_LOAD_EXTRA:-}" | tr -d ' \n')"
for img in ${IMAGES//,/ }; do
  [ -n "$img" ] || continue
  kind load docker-image --name "$NAME" "${REG}/${img}:${TAG}"
done

# Bridge services: Endpoints for compose services that pods must reach (no DNS bridge between Docker and
# cluster DNS). IPs are read from the compose networks at bootstrap time.
ip_of() { docker inspect -f "{{(index .NetworkSettings.Networks \"$2\").IPAddress}}" "$1"; }
kubectl --context "$CTX" create namespace replicant-runner --dry-run=client -o yaml | kubectl --context "$CTX" apply -f -
bridge() { # name port container network
  local ip; ip="$(ip_of "$3" "$4")"
  cat <<YAML | kubectl --context "$CTX" apply -f -
apiVersion: v1
kind: Service
metadata: {name: $1, namespace: replicant-runner, labels: {replicant.example.com/bridge: "true"}}
spec: {ports: [{name: p, port: $2, targetPort: $2}]}
---
apiVersion: discovery.k8s.io/v1
kind: EndpointSlice
metadata:
  name: $1-bridge
  namespace: replicant-runner
  labels: {kubernetes.io/service-name: $1, endpointslice.kubernetes.io/managed-by: replicant-dev}
addressType: IPv4
ports: [{name: p, port: $2}]
endpoints: [{addresses: ["$ip"]}]
YAML
}
S3_CONTAINER=replicant-s3-1; docker inspect "$S3_CONTAINER" >/dev/null 2>&1 || S3_CONTAINER=replicant-s3-minio-1
bridge temporal 7233 replicant-temporal-1 replicant-core
bridge s3 8333 "$S3_CONTAINER" replicant-core
# Core run-grant JWKS and revocation list for the kind runner-api and controller (C-RUNNER 3.0.0, R1-253).
bridge grants 8403 replicant-run-grants-1 replicant-core
bridge sbx-edge 80 replicant-sbx-edge-1 replicant-sbx
bridge sbx-dns 53 replicant-sbx-dns-1 replicant-sbx

# Cluster DNS: forward sbx.local to sbx-dns so pods resolve the same sandbox names as runner-local sandboxes.
SBX_DNS_IP="$(ip_of replicant-sbx-dns-1 replicant-sbx)"
kubectl --context "$CTX" -n kube-system get configmap coredns -o yaml | \
  sed "s|    .:53 {|    sbx.local:53 {\n        forward . ${SBX_DNS_IP}\n    }\n    .:53 {|" | \
  kubectl --context "$CTX" apply -f -

# CRD (contract file, unmodified) + controller, runner-api, run-notifier (WS-02 chart, local values).
kubectl --context "$CTX" apply -f /contracts/agentrun-crd.yaml
kubectl --context "$CTX" create secret generic runner-local-keys -n replicant-runner \
  --from-file=run_token_signing_key=/run/secrets/run_token_signing_key \
  --from-file=runner_worker_tokens=/run/secrets/runner_worker_tokens \
  --from-file=s3_runner_secret=/run/secrets/s3_runner_secret \
  --from-file=temporal_codec_key=/run/secrets/temporal_codec_key \
  --from-file=runner_revocations_readers=/run/secrets/runner_revocations_readers --dry-run=client -o yaml | \
  kubectl --context "$CTX" apply -f -
helm upgrade --install replicant-exec /helm/replicant-exec --namespace replicant-runner --kube-context "$CTX" \
  -f /helm/replicant-exec/values-local-kind.yaml \
  --set image.registry="$REG" --set image.tag="$TAG" --set temporal.address=temporal.replicant-runner.svc:7233 \
  --set runnerApi.service.nodePort=30480 \
  --set runnerApi.sandboxListener.nodePort=30490 \
  --set runnerApi.sandboxListener.publicBaseUrl=http://runner-sandbox-k8s.sbx.local \
  --set grants.issuer=replicant-local-core \
  --set grants.jwksUrl=http://grants.replicant-runner.svc:8403/.well-known/jwks.json \
  --set grants.revocationUrl=http://grants.replicant-runner.svc:8403/grants/revoked \
  --set runnerApi.revocations.readersSecretKey=runner_revocations_readers \
  --set runnerApi.revocations.bucket=local-replicant-local-ops \
  --set runnerApi.revocations.prefix=exec-revocations/local/ \
  --set isolation.degraded="$([ "$KATA" = on ] && echo false || echo true)" --wait
# Tenant namespaces from WS-09's tenant-namespace.yaml, the single schema runner-api A-5/RS-5, the controller watch and
# the VAPs key on (XD-FIX-R1-031): zone=sandbox, tenant=<t>, tenant-ready="true", exec-kill-switch="false",
# max-active-runs (local: 2), isolation-group shared-standard, plus the sbx-runner ServiceAccount and ResourceQuota.
for t in payments local-dev replicant-eval; do
  sed -e "s/sbx-payments/sbx-$t/g" -e "s/replicant.example.com\/tenant: payments/replicant.example.com\/tenant: $t/" \
      -e 's/replicant.example.com\/max-active-runs: "[0-9]*"/replicant.example.com\/max-active-runs: "2"/' \
      /security/kubernetes/tenant-namespace.yaml | kubectl --context "$CTX" apply -f -
done
kubectl --context "$CTX" -n replicant-runner rollout status deploy/runner-api deploy/agentrun-controller deploy/run-notifier
echo "k8s profile ready: runner-api at http://${NAME}-control-plane:30480 (host: http://127.0.0.1:8481); sandbox listener via runner-sandbox-k8s.sbx.local"
