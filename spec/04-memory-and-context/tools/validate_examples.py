"""Validate WS-04 JSON Schemas (meta) and every example. Run: uv run --with jsonschema --with pyyaml --with rfc3339-validator python tools/validate_examples.py"""
import json, sys, pathlib, yaml
from jsonschema import Draft202012Validator, FormatChecker
B = pathlib.Path(__file__).resolve().parent.parent
C = B.parent / "00-contracts"
ok = True
schemas = {p.name: json.loads(p.read_text()) for p in (B/"schemas").glob("*.json")}
for n, s in schemas.items():
    Draft202012Validator.check_schema(s); print("meta-valid", n)
pairs = [("handoff.schema.json","handoff.example.json"),("memory-injection.schema.json","memory-injection.example.json"),
         ("memory-candidates.schema.json","memory-candidates.example.json"),("briefing.schema.json","briefing.example.json"),
         ("context-assembly-report.schema.json","context-assembly-report.example.json"),("memory-governance.schema.json","governance-default.json")]
for s, e in pairs:
    errs = list(Draft202012Validator(schemas[s], format_checker=FormatChecker()).iter_errors(json.loads((B/"examples"/e).read_text())))
    print(("OK  " if not errs else "FAIL"), e, [f"{list(x.path)}: {x.message}" for x in errs][:5]); ok &= not errs
sb = json.loads((B/"examples/steering-stage-backlog.example.json").read_text())
sbs = schemas["steering-stage-backlog.schema.json"]
for part, sch in [("input", sbs), ("result", {**sbs, "$ref": "#/$defs/result", "required": [], "properties": {}, "additionalProperties": True})]:
    errs = list(Draft202012Validator(sch, format_checker=FormatChecker()).iter_errors(sb[part]))
    print(("OK  " if not errs else "FAIL"), "steering-stage-backlog.example.json", part, [x.message for x in errs][:5]); ok &= not errs
ids = sb["input"]["message_ids"]; ok &= ids == [m["message_id"] for m in sb["input"]["messages"]]
doc = yaml.safe_load((B/"examples/policy-memory-snapshot-default.yaml").read_text())
env = json.loads((C/"schemas/definition-envelope.schema.json").read_text())
for name, sch, inst in [("envelope", env, doc), ("policy spec", schemas["policy-memory-snapshot.schema.json"], doc["spec"])]:
    errs = list(Draft202012Validator(sch).iter_errors(inst)); print(("OK  " if not errs else "FAIL"), "policy-memory-snapshot-default.yaml", name, [e.message for e in errs][:5]); ok &= not errs
# memory_injection refs in handoff must match injection
sys.exit(0 if ok else 1)
