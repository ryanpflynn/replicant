"""Vendored, byte-identical copies of the spec files the worker reads at runtime and in tests.

`resources/spec/` mirrors the layout of the spec repository (never edited here; re-copy to update):
01-orchestration/{graphs/definitions,policies,schemas,codec}, 00-contracts (runner API, JobRequest, schemas),
03-agents-and-tools/schemas, 04-memory-and-context (steering backlog, memory-service API), 05-qa/schemas,
07-control-plane-and-ux/schemas/policy-gate-rules.schema.json.
"""
from __future__ import annotations

import json
import pathlib
from functools import lru_cache
from typing import Any

import yaml

SPEC = pathlib.Path(__file__).resolve().parent / "spec"
ORCH = SPEC / "01-orchestration"  # == the reference's ROOT
CONTRACTS = SPEC / "00-contracts"


@lru_cache(maxsize=None)
def load_json(rel: str) -> Any:
    return json.loads((SPEC / rel).read_text())


@lru_cache(maxsize=None)
def load_yaml(rel: str) -> Any:
    return yaml.safe_load((SPEC / rel).read_text())
