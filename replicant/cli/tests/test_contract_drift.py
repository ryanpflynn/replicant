"""Vendored contract files never drift: every copy of a schema or OpenAPI file in the places that vendor them is
byte-identical (contracts/ is the verbatim copy of spec/00-contracts; see contracts/README.md)."""
from __future__ import annotations

from collections import defaultdict
from pathlib import Path

from replicant_cli.paths import repo_root

ROOT = repo_root()
VENDORED = [
    "contracts",                                                   # the repo's copy of spec/00-contracts
    "go/internal/runner/schemas/files",                            # runner-local, entrypoint (go:embed)
    "services/control-plane-api/src/control_plane_api/schemas",
    "workers/replicant-projector/replicant_projector/schemas",
    "workers/replicant-worker/replicant_worker/resources/spec",    # the worker's spec copy
]
SUFFIXES = (".schema.json", ".openapi.yaml", "-crd.yaml")


def _copies() -> dict[str, list[Path]]:
    by_name: dict[str, list[Path]] = defaultdict(list)
    for d in VENDORED:
        for f in sorted((ROOT / d).rglob("*")):
            if f.is_file() and f.name.endswith(SUFFIXES):
                by_name[f.name].append(f)
    return by_name


def test_vendored_contracts_are_byte_identical():
    shared = {n: fs for n, fs in _copies().items() if len(fs) > 1}
    drift = [f"{n}: " + ", ".join(str(f.relative_to(ROOT)) for f in fs)
             for n, fs in sorted(shared.items()) if len({f.read_bytes() for f in fs}) > 1]
    assert not drift, "vendored copies differ (re-copy from spec/00-contracts):\n" + "\n".join(drift)


def test_every_contract_is_vendored_where_expected():
    names = _copies()
    for f in [*(ROOT / "contracts").glob("*.json"), *(ROOT / "contracts" / "schemas").glob("*.json"),
              ROOT / "contracts" / "runner-api.openapi.yaml"]:
        assert len(names[f.name]) >= 2, f"{f.name} has no vendored copy (is the drift test looking in the right place?)"
