#!/usr/bin/env python3
"""Validate WS-07 policy examples against the frozen C-DEF envelope and the WS-07 kind schemas.
uv run --with 'jsonschema>=4,<5' --with pyyaml python schemas/validate_policies.py"""
import json, pathlib, sys, yaml
from jsonschema import Draft202012Validator
HERE = pathlib.Path(__file__).parent
ENV = json.loads((HERE.parents[1] / "00-contracts/schemas/definition-envelope.schema.json").read_text())
KIND = {p: json.loads((HERE / f"policy-{p.replace('_','-')}.schema.json").read_text()) for p in ["risk_rules", "gate_rules", "approval_channels"]}
errs = []
for f in sorted((HERE / "examples").glob("*.yaml")):
    doc = yaml.safe_load(f.read_text())
    for e in Draft202012Validator(ENV).iter_errors(doc): errs.append(f"{f.name}: envelope: {e.message}")
    for e in Draft202012Validator(KIND[doc["spec"]["policyType"]]).iter_errors(doc["spec"]): errs.append(f"{f.name}: spec: {e.message} at {list(e.absolute_path)}")
for s in KIND.values(): Draft202012Validator.check_schema(s)
print("\n".join(errs) or "all policy examples valid (envelope + kind schema); kind schemas meta-valid")
sys.exit(1 if errs else 0)
