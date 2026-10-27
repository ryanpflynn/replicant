"""Validate WS-04 MCP tool definitions against the official MCP 2026-07-28 Tool schema.
Run: uv run --with jsonschema python tools/validate_mcp.py [path/to/schema.json]"""
import json, re, sys
from jsonschema import Draft202012Validator
from referencing import Registry, Resource
import pathlib
HERE = pathlib.Path(__file__).resolve().parent
# Download first: curl -sfL -o mcp-2026-07-28.schema.json https://raw.githubusercontent.com/modelcontextprotocol/modelcontextprotocol/main/schema/2026-07-28/schema.json
mcp = json.load(open(sys.argv[1] if len(sys.argv) > 1 else HERE / "mcp-2026-07-28.schema.json"))
reg = Registry().with_resource("urn:mcp", Resource.from_contents(mcp))
tool_schema = {"$schema": "https://json-schema.org/draft/2020-12/schema", "$ref": "urn:mcp#/$defs/Tool"}
v = Draft202012Validator(tool_schema, registry=reg)
ok = True
for f in ["memory-mcp.tools.json", "context-mcp.tools.json"]:
    d = json.load(open(HERE.parent / "mcp" / f))
    for t in d["tools"]:
        errs = [e.message for e in v.iter_errors(t)]
        Draft202012Validator.check_schema(t["inputSchema"]); Draft202012Validator.check_schema(t["outputSchema"])
        if not re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", t["name"]): errs.append("bad name")
        print("OK  " if not errs else "FAIL", f, t["name"], errs[:3]); ok &= not errs
sys.exit(0 if ok else 1)
