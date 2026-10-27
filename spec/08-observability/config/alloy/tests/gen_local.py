#!/usr/bin/env python3
"""Regenerate ../local.alloy from ../otlp-gateway-core.alloy and ../node-logs.alloy (WS-08).

The processing stages are copied verbatim so local and production pipelines cannot drift. CI runs this script
and fails if `git diff --exit-code ../local.alloy` is non-empty. The Docker-discovery header is kept in
LOCAL_HEADER below (copied from the current local.alloy on first run).
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOCAL = ROOT / "local.alloy"
MARK_PROCS = "// Sandbox pipeline"
MARK_OTLP = "// OTLP (identical to otlp-gateway-core.alloy from here on, TLS removed)"


def main() -> None:
    core = (ROOT / "otlp-gateway-core.alloy").read_text()
    nl = (ROOT / "node-logs.alloy").read_text()
    current = LOCAL.read_text()
    header = current[: current.index(MARK_PROCS)]
    write_block = current[current.index('loki.write "default"'): current.index(MARK_OTLP) + len(MARK_OTLP)]
    write_block = write_block + "\n" + "// " + "-" * 111 + "\n"
    c = core[core.index('otelcol.receiver.otlp "default" {'):]
    c = re.sub(r"\n\n    tls \{\n      ca_file\s+= .*?\n    \}\n", "\n", c, flags=re.S)
    c = c.replace('    cluster = "core",', '    cluster = "local",')
    procs = nl[nl.index(MARK_PROCS): nl.index('loki.write "default"')].replace("  stage.cri {}\n\n", "").replace("  stage.cri { }\n\n", "")
    procs = procs.replace('"/etc/replicant/redaction/gitleaks-replicant.toml"',
                          'sys.env("REPLICANT_REDACTION_DIR") + "/gitleaks-replicant.toml"')
    LOCAL.write_text(header + procs.rstrip("\n") + "\n\n" + write_block + c)


if __name__ == "__main__":
    main()
