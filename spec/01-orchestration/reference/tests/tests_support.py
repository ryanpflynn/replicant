"""Shared fixtures for unit and scenario tests."""
LICENSE_MODIFIER = {  # one entry of tenant payments' codebase binding graphModifiers (extensibility.md section 6)
    "op": "insertBefore", "anchor": "implement", "source": "policy/codebase-bindings.payments@4",
    "node": {"key": "license_scan", "type": "license_policy_scan", "phase": "pre_checks", "maxVisits": 2,
             "config": {"deny": ["AGPL-3.0-only", "SSPL-1.0", "BUSL-1.1"], "failOn": "deny", "ecosystems": ["go"]}},
    "branches": [{"when": "nodes['license_scan'].outcome == 'violations'", "to": "end:policy_violation"}],
}
