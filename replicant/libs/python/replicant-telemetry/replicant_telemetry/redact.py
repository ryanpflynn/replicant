"""Source-side redaction for Python services (WS-08, R1-198). Used by replicant_telemetry.JsonLogFormatter for the
log message and string extras, and by redact_preview() for LLM input/output previews of worker activities.

Rules loaded:
  * Replicant rules of redaction/gitleaks-replicant.toml (the [[rules]] blocks; regex syntax is RE2-compatible),
  * the PII rules of redaction/pii-rules.yaml (replace rules, with validators),
  * known values registered at runtime (service credentials, if any).
The ~220 upstream gitleaks default rules are not re-implemented in Python; Alloy's loki.secretfilter applies them
to every log line as the second pass (ADR-08-008). Python services wrap their stdout formatter in RedactingFormatter.
v0: Go services have no source-side log redactor yet (the sandbox uploader redacts transcripts; Alloy is the log pass).

Self-test: uv run --with pyyaml python redact.py
"""
from __future__ import annotations

import hashlib
import logging
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import yaml

_HERE = Path(__file__).resolve().parent
DEFAULT_TOML = _HERE / "redaction" / "gitleaks-replicant.toml"
DEFAULT_PII = _HERE / "redaction" / "pii-rules.yaml"


def fingerprint(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()[:12]


def _luhn(v: str) -> bool:
    ds = [int(c) for c in v if c.isdigit()]
    if not 13 <= len(ds) <= 19:
        return False
    total = 0
    for i, d in enumerate(reversed(ds)):
        if i % 2:
            d = d * 2 - 9 if d * 2 > 9 else d * 2
        total += d
    return total % 10 == 0


def _ssn(v: str) -> bool:
    a, g, s = v.split("-")
    return a not in ("000", "666") and not a.startswith("9") and g != "00" and s != "0000"


_VALIDATORS: dict[str, Callable[[str], bool]] = {
    "luhn": _luhn, "us_ssn": _ssn, "iban_mod97": lambda v: True, "public_ipv4": lambda v: True,
}


@dataclass
class _Rule:
    rule_id: str
    rx: re.Pattern
    group: int
    pii: bool
    allow: list[re.Pattern]
    validator: Callable[[str], bool] | None
    keywords: list[str] | None
    window: int


class Redactor:
    def __init__(self, toml_path: Path = DEFAULT_TOML, pii_path: Path = DEFAULT_PII):
        cfg = tomllib.loads(toml_path.read_text())
        self.version = cfg.get("title", "").split()[-1]
        self.rules: list[_Rule] = []
        for r in cfg.get("rules", []):
            self.rules.append(_Rule(r["id"], re.compile(r["regex"]), int(r.get("secretGroup", 0)), False,
                                    [], None, None, 0))
        self.allow = [re.compile(a) for blk in cfg.get("allowlists", []) for a in blk.get("regexes", [])]
        pii = yaml.safe_load(pii_path.read_text())
        for r in pii["rules"]:
            if r["action"] != "replace":
                continue
            self.rules.append(_Rule(r["id"], re.compile(r["regex"]), 0, True,
                                    [re.compile(a) for a in r.get("allow", [])],
                                    _VALIDATORS.get(r.get("validator", "")), r.get("context_keywords"),
                                    r.get("context_window", 32)))
        self.known: list[str] = []

    def set_known_values(self, values: list[str]) -> None:
        self.known = sorted({v for v in values if len(v) >= 8}, key=len, reverse=True)

    def redact(self, text: str) -> tuple[str, list[tuple[str, str]]]:
        """Returns (redacted text, [(rule_id, fingerprint or '')]). Never raises on content."""
        findings: list[tuple[str, str]] = []
        for v in self.known:
            if v in text:
                fp = fingerprint(v)
                text = text.replace(v, f"[REDACTED:known-credential:{fp}]")
                findings.append(("known-credential", fp))
        for rule in self.rules:
            def repl(m: re.Match, rule: _Rule = rule) -> str:
                secret = m.group(rule.group) if rule.group else m.group(0)
                if any(a.search(secret) for a in self.allow) or any(a.search(secret) for a in rule.allow):
                    return m.group(0)
                if rule.validator and not rule.validator(secret):
                    return m.group(0)
                if rule.keywords:
                    window = m.string[max(0, m.start() - rule.window):m.start()].lower()
                    if not any(k in window for k in rule.keywords):
                        return m.group(0)
                if rule.pii:
                    findings.append((rule.rule_id, ""))
                    return f"[REDACTED:{rule.rule_id}]"
                fp = fingerprint(secret)
                findings.append((rule.rule_id, fp))
                token = f"[REDACTED:{rule.rule_id}:{fp}]"
                if rule.group:
                    s, e = m.span(rule.group)
                    return m.group(0)[: s - m.start()] + token + m.group(0)[e - m.start():]
                return token
            text = rule.rx.sub(repl, text)
        return text, findings


_default: Redactor | None = None


def default_redactor() -> Redactor:
    global _default
    if _default is None:
        _default = Redactor()
    return _default


def redact_text(text: str) -> str:
    """Fail closed: any error replaces the whole value (session-capture.md 5.3)."""
    try:
        return default_redactor().redact(text)[0]
    except Exception:  # noqa: BLE001 - fail closed by design
        return f"[REDACTION_FAILED:{len(text)}]"


def redact_preview(text: str, limit: int) -> str:
    out = redact_text(text)
    return out if len(out) <= limit else out[:limit] + f"…[truncated {len(out) - limit} bytes]"


if __name__ == "__main__":
    r = Redactor()
    jwt = "eyJhbGciOiJFUzI1NiJ9.eyJzdWIiOiJydW46cnVuX2FiYyJ9.c2lnbmF0dXJlLXNpZ25hdHVyZQ"
    out, f = r.redact(f"calling memory-mcp with Authorization: Bearer {jwt} for dana.whitfield@acme-payments.com")
    assert jwt not in out and "dana.whitfield" not in out, out
    assert "job_01m5p7x7r0bt69j0h0fbxepw90" in r.redact("job job_01m5p7x7r0bt69j0h0fbxepw90 ok")[0]
    r.set_known_values(["s3cr3t-run-token-value"])
    assert "s3cr3t" not in r.redact("token=s3cr3t-run-token-value")[0]
    assert redact_preview("x" * 50, 10).startswith("xxxxxxxxxx…")
    print("redact self-test passed:", out)


class RedactingFormatter(logging.Formatter):
    """Wraps a service's formatter and redacts the whole formatted line: message, extras and tracebacks."""

    def __init__(self, inner: logging.Formatter) -> None:
        super().__init__()
        self.inner = inner

    def format(self, record: logging.LogRecord) -> str:
        return redact_text(self.inner.format(record))
