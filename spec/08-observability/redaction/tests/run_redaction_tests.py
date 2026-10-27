#!/usr/bin/env python3
"""Redaction conformance harness (WS-08), ruleset redact-2026.10.1.

Expands corpus.yaml placeholders with seeded fake secrets, then checks three implementations:

  gitleaks  `gitleaks stdin -c gitleaks-replicant.toml --report-format json` (same Go detector the sandbox
            entrypoint, shipper and leak-scanner embed). A case passes when every generated secret is
            covered by a finding (redacted) or no finding is reported (unchanged).
  alloy     `alloy run` with loki.secretfilter + gitleaks-replicant.toml, reading the expanded corpus as CRI
            log lines and printing through loki.echo. A case passes when no generated secret survives
            (redacted) or the line is byte-identical (unchanged).
  pii       reference implementation of pii-rules.yaml in this file (RE2-compatible regexes only, so the
            Go port in the entrypoint uses identical patterns).

Usage (CI and local):
  uv run --with pyyaml python run_redaction_tests.py --gitleaks /path/gitleaks --alloy /path/alloy

Exit code 0 only if every case passes for every sink it lists.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import random
import re
import string
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
RULES = HERE.parent / "gitleaks-replicant.toml"
PII = HERE.parent / "pii-rules.yaml"

B32 = string.ascii_uppercase + "234567"
ALNUM = string.ascii_letters + string.digits


def _gen(rng: random.Random) -> dict[str, callable]:
    def r(n: int, alphabet: str = ALNUM) -> str:
        return "".join(rng.choice(alphabet) for _ in range(n))

    def b64url(raw: bytes) -> str:
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()

    def jwt() -> str:
        header = b64url(json.dumps({"alg": "ES256", "kid": r(8)}).encode())
        claims = b64url(json.dumps({"sub": "run:run_" + r(26, "0123456789abcdefghjkmnpqrstvwxyz"),
                                    "aud": ["memory-mcp"], "exp": 1792822345}).encode())
        return f"{header}.{claims}.{b64url(bytes(rng.getrandbits(8) for _ in range(64)))}"

    return {
        "ghp": lambda: "ghp_" + r(36),
        "ghs": lambda: "ghs_" + r(36),
        "akia_b32": lambda: "AKIA" + r(16, B32),
        "akia_broad": lambda: "ASIA" + r(16, string.ascii_uppercase + string.digits),
        "aws_secret": lambda: r(40, ALNUM + "/+"),
        "aws_session": lambda: r(300, ALNUM + "/+") + "==",
        "jwt": jwt,
        "opaque40": lambda: r(40, ALNUM + "-_"),
        "xoxb": lambda: f"xoxb-{r(11, string.digits)}-{r(13, string.digits)}-{r(24)}",
        "pem_body": lambda: "\\n".join(r(64, ALNUM + "+/") for _ in range(6)),
        "password16": lambda: r(16, ALNUM + "!#%^*"),
        "hex64": lambda: r(64, "0123456789abcdef"),
        "sk_lf": lambda: "sk-lf-" + "-".join(r(n, "0123456789abcdef") for n in (8, 4, 4, 4, 12)),
    }


PLACEHOLDER = re.compile(r"\{\{([a-z0-9_]+)\}\}")


def expand(cases: list[dict], seed: int) -> list[dict]:
    rng = random.Random(seed)
    gens = _gen(rng)
    out = []
    for case in cases:
        secrets: list[str] = []

        def sub(m: re.Match) -> str:
            value = gens[m.group(1)]()
            # pem bodies are judged by their first line only (multi-line redaction is per rule)
            secrets.append(value.split("\\n")[0])
            return value

        line = PLACEHOLDER.sub(sub, case["input"])
        out.append({**case, "line": line, "secrets": secrets})
    return out


# ----------------------------------------------------------------------------------------------
# sink: gitleaks
# ----------------------------------------------------------------------------------------------
def check_gitleaks(binary: str, case: dict) -> tuple[bool, str]:
    with tempfile.TemporaryDirectory() as td:
        report = Path(td) / "r.json"
        proc = subprocess.run(
            [binary, "stdin", "-c", str(RULES), "--no-banner", "--log-level", "error",
             "--report-format", "json", "--report-path", str(report), "--exit-code", "0"],
            input=case["line"].encode(), capture_output=True, timeout=60,
        )
        if proc.returncode != 0:
            return False, f"gitleaks exit {proc.returncode}: {proc.stderr.decode()[:300]}"
        findings = json.loads(report.read_text() or "[]")
    if case["expect"] == "unchanged":
        return (not findings), f"false positive: {[f['RuleID'] for f in findings]}"
    spans = [f.get("Match", "") + " " + f.get("Secret", "") for f in findings]
    missed = [s for s in case["secrets"] if not any(s[:12] in sp for sp in spans)]
    return (not missed), f"missed {len(missed)} secret(s); rules fired: {[f['RuleID'] for f in findings]}"


# ----------------------------------------------------------------------------------------------
# sink: alloy loki.secretfilter
# ----------------------------------------------------------------------------------------------
ALLOY_CFG = """
local.file_match "c" {{ path_targets = [{{"__path__" = "{path}", "job" = "redaction-test"}}] }}
loki.source.file "c" {{
  targets       = local.file_match.c.targets
  forward_to    = [loki.process.c.receiver]
  tail_from_end = false
}}
loki.process "c" {{
  forward_to = [loki.secretfilter.c.receiver]
  stage.cri {{}}
}}
loki.secretfilter "c" {{
  forward_to      = [loki.echo.c.receiver]
  gitleaks_config = "{rules}"
  redact_with     = "[REDACTED:$SECRET_NAME:$SECRET_HASH]"
}}
loki.echo "c" {{}}
"""


def run_alloy(binary: str, cases: list[dict]) -> dict[str, str]:
    with tempfile.TemporaryDirectory() as td:
        log = Path(td) / "in.log"
        with log.open("w") as f:
            for i, c in enumerate(cases):
                f.write(f"2026-10-24T08:00:{i % 60:02d}.{i:09d}Z stdout F {c['id']}|{c['line']}\n")
        cfg = Path(td) / "c.alloy"
        cfg.write_text(ALLOY_CFG.format(path=log, rules=RULES))
        out = Path(td) / "out.log"
        with out.open("w") as fh:
            p = subprocess.Popen([binary, "run", str(cfg), f"--storage.path={td}/data",
                                  "--server.http.listen-addr=127.0.0.1:12397"], stdout=fh, stderr=subprocess.STDOUT)
            deadline = time.time() + 30
            while time.time() < deadline and out.read_text().count("received log entry") < len(cases):
                time.sleep(0.5)
            p.terminate()
            p.wait(timeout=10)
        result: dict[str, str] = {}
        for line in out.read_text().splitlines():
            if "received log entry" not in line:
                continue
            entry = json.loads(line).get("entry") if line.startswith("{") else \
                re.search(r'entry="((?:[^"\\]|\\.)*)"', line).group(1).encode().decode("unicode_escape")
            cid, _, body = entry.partition("|")
            result[cid] = body
        return result


def check_alloy(outputs: dict[str, str], case: dict) -> tuple[bool, str]:
    body = outputs.get(case["id"])
    if body is None:
        return False, "no output line"
    if case["expect"] == "unchanged":
        return body == case["line"], f"changed to: {body[:160]}"
    leaked = [s for s in case["secrets"] if s in body]
    return (not leaked), f"secret survived: {body[:160]}"


# ----------------------------------------------------------------------------------------------
# sink: pii (reference implementation of pii-rules.yaml)
# ----------------------------------------------------------------------------------------------
def luhn(digits: str) -> bool:
    ds = [int(c) for c in digits if c.isdigit()]
    if not 13 <= len(ds) <= 19:
        return False
    total = 0
    for i, d in enumerate(reversed(ds)):
        if i % 2:
            d = d * 2 - 9 if d * 2 > 9 else d * 2
        total += d
    return total % 10 == 0


def us_ssn(v: str) -> bool:
    area, group, serial = v.split("-")
    return area not in ("000", "666") and not area.startswith("9") and group != "00" and serial != "0000"


VALIDATORS = {"luhn": luhn, "us_ssn": us_ssn, "iban_mod97": lambda v: True, "public_ipv4": lambda v: True}


def apply_pii(line: str, rules: dict) -> str:
    for rule in rules["rules"]:
        if rule["action"] != "replace":
            continue
        rx = re.compile(rule["regex"])
        allow = [re.compile(a) for a in rule.get("allow", [])]
        kws = rule.get("context_keywords")

        def repl(m: re.Match) -> str:
            v = m.group(0)
            if any(a.search(v) for a in allow):
                return v
            if rule.get("validator") and not VALIDATORS[rule["validator"]](v):
                return v
            if kws:
                window = line[max(0, m.start() - rule.get("context_window", 32)):m.start()].lower()
                if not any(k in window for k in kws):
                    return v
            return f"[REDACTED:{rule['id']}]"

        line = rx.sub(repl, line)
    return line


def check_pii(rules: dict, case: dict) -> tuple[bool, str]:
    out = apply_pii(case["line"], rules)
    if case["expect"] == "unchanged":
        return out == case["line"], f"changed to: {out}"
    return out != case["line"] and "[REDACTED:" in out, f"not redacted: {out}"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--gitleaks", default=os.environ.get("GITLEAKS_BIN", "gitleaks"))
    ap.add_argument("--alloy", default=os.environ.get("ALLOY_BIN", "alloy"))
    ap.add_argument("--seed", type=int, default=20261024)
    args = ap.parse_args()

    corpus = yaml.safe_load((HERE / "corpus.yaml").read_text())
    pii_rules = yaml.safe_load(PII.read_text())
    cases = expand(corpus["cases"], args.seed)
    alloy_cases = [c for c in cases if "alloy" in c["sinks"]]
    alloy_out = run_alloy(args.alloy, alloy_cases) if alloy_cases else {}

    failures = 0
    for case in cases:
        for sink in case["sinks"]:
            if sink == "gitleaks":
                ok, why = check_gitleaks(args.gitleaks, case)
            elif sink == "alloy":
                ok, why = check_alloy(alloy_out, case)
            else:
                ok, why = check_pii(pii_rules, case)
            print(f"{'PASS' if ok else 'FAIL'}  {sink:8s} {case['id']}" + ("" if ok else f"  -- {why}"))
            failures += 0 if ok else 1
    print(f"\n{failures} failure(s); ruleset {corpus['ruleset_version']}; fingerprint of corpus seed "
          f"{hashlib.sha256(str(args.seed).encode()).hexdigest()[:12]}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
