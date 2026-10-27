# ADR-02-005: Egress control with Cilium FQDN policy (no general forward proxy)

- **Status:** Accepted (WS-02, 2026-10-24); reviewed by WS-09 (SC-07).
- **Constraints:** D-21 (default-deny egress; mirror/proxy or FQDN-aware policy)

## Context

Sandboxes need a handful of internal destinations plus a package mirror. Plain NetworkPolicy cannot
express FQDNs. Verified: Cilium enforces `toFQDNs` through its DNS proxy and refuses non-allowlisted
queries (`--tofqdns-dns-reject-response-code` default `refused`); in CNI chaining with the AWS VPC
CNI, Cilium lists L7 policy as limited; Cilium's socket LB is ineffective for Kata and gVisor and
needs `socketLB.hostNamespaceOnly=true`; the standalone DNS proxy is alpha.

## Options

1. Envoy/Squid forward proxy with SNI allowlists plus deny-all NetworkPolicy.
2. **Cilium (ENI mode, sole CNI) with per-run `toFQDNs` policies and DNS allowlists, plus the
   package mirror as the only package source (chosen).**
3. Both (proxy for HTTP(S), Cilium for L3/L4).

## Decision

Option 2, with a cluster-wide default-deny baseline and explicit `egressDeny` for IMDS, the Pod
Identity agent, kube-apiserver, host, and remote nodes; AWS security groups and route tables
without a default route as a second layer.

## Consequences

- No proxy to scale or to be bypassed by tools that ignore `HTTPS_PROXY`.
- DNS proxy availability is a node-level dependency (drain before Cilium upgrades).
- Arbitrary public hosts require a new profile version (bundle change, WS-09 approval).

## Revisit when

- A profile needs many public hosts (add an SNI-filtering Envoy for that profile only), or the
  standalone DNS proxy becomes stable.
